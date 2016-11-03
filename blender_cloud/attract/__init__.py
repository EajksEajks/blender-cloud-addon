# ##### BEGIN GPL LICENSE BLOCK #####
#
#  This program is free software; you can redistribute it and/or
#  modify it under the terms of the GNU General Public License
#  as published by the Free Software Foundation; either version 2
#  of the License, or (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program; if not, write to the Free Software Foundation,
#  Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301, USA.
#
# ##### END GPL LICENSE BLOCK #####

# <pep8 compliant>

# Old info, kept here for reference, so that we can merge wiki pages,
# descriptions, etc.
#
# bl_info = {
#     "name": "Attract",
#     "author": "Francesco Siddi, Inês Almeida, Antony Riakiotakis",
#     "version": (0, 2, 0),
#     "blender": (2, 76, 0),
#     "location": "Video Sequence Editor",
#     "description":
#         "Blender integration with the Attract task tracking service"
#         ". *requires the Blender ID add-on",
#     "wiki_url": "http://wiki.blender.org/index.php/Extensions:2.6/Py/"
#                 "Scripts/Workflow/Attract",
#     "category": "Workflow",
#     "support": "TESTING"
# }

import contextlib
import functools
import logging

if "bpy" in locals():
    import importlib

    draw = importlib.reload(draw)
    pillar = importlib.reload(pillar)
    async_loop = importlib.reload(async_loop)
else:
    from . import draw
    from .. import pillar, async_loop

import bpy
import pillarsdk
from pillarsdk.nodes import Node
from pillarsdk.projects import Project
from pillarsdk import exceptions as sdk_exceptions

from bpy.types import Operator, Panel, AddonPreferences

log = logging.getLogger(__name__)


def active_strip(context):
    try:
        return context.scene.sequence_editor.active_strip
    except AttributeError:
        return None


def selected_shots(context):
    """Generator, yields selected strips if they are Attract shots."""

    for strip in context.selected_sequences:
        atc_object_id = getattr(strip, 'atc_object_id')
        if not atc_object_id:
            continue

        yield strip


def remove_atc_props(strip):
    """Resets the attract custom properties assigned to a VSE strip"""

    strip.atc_name = ""
    strip.atc_description = ""
    strip.atc_object_id = ""
    strip.atc_is_synced = False


class ToolsPanel(Panel):
    bl_label = 'Attract'
    bl_space_type = 'SEQUENCE_EDITOR'
    bl_region_type = 'UI'

    def draw_header(self, context):
        strip = active_strip(context)
        if strip and strip.atc_object_id:
            self.layout.prop(strip, 'atc_is_synced', text='')

    def draw(self, context):
        strip = active_strip(context)
        layout = self.layout
        strip_types = {'MOVIE', 'IMAGE'}

        selshots = list(selected_shots(context))
        if strip and strip.type in strip_types and strip.atc_object_id:
            if len(selshots) > 1:
                noun = 'selected shots'
            else:
                noun = 'this shot'

            layout.prop(strip, 'atc_name', text='Name')
            layout.prop(strip, 'atc_status', text='Status')

            # Create a special sub-layout for read-only properties.
            ro_sub = layout.column(align=True)
            ro_sub.enabled = False
            ro_sub.prop(strip, 'atc_description', text='Description')
            ro_sub.prop(strip, 'atc_notes', text='Notes')

            if strip.atc_is_synced:
                sub = layout.column(align=True)
                row = sub.row(align=True)
                row.operator('attract.submit_selected', text='Submit %s' % noun)
                row.operator(AttractShotFetchUpdate.bl_idname,
                             text='', icon='FILE_REFRESH')
                row.operator(ATTRACT_OT_shot_open_in_browser.bl_idname,
                             text='', icon='WORLD')
                sub.operator(ATTRACT_OT_make_shot_thumbnail.bl_idname)

                # Group more dangerous operations.
                dangerous_sub = layout.column(align=True)
                dangerous_sub.operator(AttractShotDelete.bl_idname)
                dangerous_sub.operator('attract.strip_unlink')

        elif context.selected_sequences:
            if len(context.selected_sequences) > 1:
                noun = 'selected strips'
            else:
                noun = 'this strip'
            layout.operator(AttractShotSubmitSelected.bl_idname,
                            text='Submit %s as new shot' % noun)
            layout.operator('attract.shot_relink')
        else:
            layout.label(text='Select a Movie or Image strip')


class AttractOperatorMixin:
    """Mix-in class for all Attract operators."""

    def _project_needs_setup_error(self):
        self.report({'ERROR'}, 'Your Blender Cloud project is not set up for Attract.')
        return {'CANCELLED'}

    @functools.lru_cache()
    def find_project(self, project_uuid: str) -> Project:
        """Finds a single project.

        Caches the result in memory to prevent more than one call to Pillar.
        """

        from .. import pillar

        project = pillar.sync_call(Project.find_one, {'where': {'_id': project_uuid}})
        return project

    def find_node_type(self, node_type_name: str) -> dict:
        from .. import pillar, blender

        prefs = blender.preferences()
        project = self.find_project(prefs.attract_project.project)

        # FIXME: Eve doesn't seem to handle the $elemMatch projection properly,
        # even though it works fine in MongoDB itself. As a result, we have to
        # search for the node type.
        node_type_list = project['node_types']
        node_type = next((nt for nt in node_type_list if nt['name'] == node_type_name), None)

        if not node_type:
            return self._project_needs_setup_error()

        return node_type

    def submit_new_strip(self, strip):
        from .. import pillar, blender

        # Define the shot properties
        user_uuid = pillar.pillar_user_uuid()
        if not user_uuid:
            self.report({'ERROR'}, 'Your Blender Cloud user ID is not known, '
                                   'update your credentials.')
            return {'CANCELLED'}

        prop = {'name': strip.name,
                'description': '',
                'properties': {'status': 'todo',
                               'notes': '',
                               'trim_start_in_frames': strip.frame_offset_start,
                               'duration_in_edit_in_frames': strip.frame_final_duration,
                               'cut_in_timeline_in_frames': strip.frame_final_start},
                'order': 0,
                'node_type': 'attract_shot',
                'project': blender.preferences().attract_project.project,
                'user': user_uuid}

        # Create a Node item with the attract API
        node = Node(prop)
        post = pillar.sync_call(node.create)

        # Populate the strip with the freshly generated ObjectID and info
        if not post:
            self.report({'ERROR'}, 'Error creating node! Check the console for now.')
            return {'CANCELLED'}

        strip.atc_object_id = node['_id']
        strip.atc_is_synced = True
        strip.atc_name = node['name']
        strip.atc_description = node['description']
        strip.atc_notes = node['properties']['notes']
        strip.atc_status = node['properties']['status']

        draw.tag_redraw_all_sequencer_editors()

    def submit_update(self, strip):
        import pillarsdk
        from .. import pillar

        patch = {
            'op': 'from-blender',
            '$set': {
                'name': strip.atc_name,
                'properties.trim_start_in_frames': strip.frame_offset_start,
                'properties.duration_in_edit_in_frames': strip.frame_final_duration,
                'properties.cut_in_timeline_in_frames': strip.frame_final_start,
                'properties.status': strip.atc_status,
            }
        }

        node = pillarsdk.Node({'_id': strip.atc_object_id})
        result = pillar.sync_call(node.patch, patch)
        log.info('PATCH result: %s', result)

    def relink(self, strip, atc_object_id, *, refresh=False):
        from .. import pillar

        try:
            node = pillar.sync_call(Node.find, atc_object_id, caching=False)
        except (sdk_exceptions.ResourceNotFound, sdk_exceptions.MethodNotAllowed):
            verb = 'refresh' if refresh else 'relink'
            self.report({'ERROR'}, 'Shot %r not found on the Attract server, unable to %s.'
                        % (atc_object_id, verb))
            strip.atc_is_synced = False
            return {'CANCELLED'}

        strip.atc_is_synced = True
        if not refresh:
            strip.atc_name = node.name
            strip.atc_object_id = node['_id']

        # We do NOT set the position/cuts of the shot, that always has to come from Blender.
        strip.atc_status = node.properties.status
        strip.atc_notes = node.properties.notes or ''
        strip.atc_description = node.description or ''

        draw.tag_redraw_all_sequencer_editors()


class AttractShotFetchUpdate(AttractOperatorMixin, Operator):
    bl_idname = "attract.shot_fetch_update"
    bl_label = "Fetch update from Attract"
    bl_description = 'Update status, description & notes from Attract'

    @classmethod
    def poll(cls, context):
        return any(selected_shots(context))

    def execute(self, context):
        for strip in selected_shots(context):
            status = self.relink(strip, strip.atc_object_id, refresh=True)
            # We don't abort when one strip fails. All selected shots should be
            # refreshed, even if one can't be found (for example).
            if not isinstance(status, set):
                self.report({'INFO'}, "Shot {0} refreshed".format(strip.atc_name))
        return {'FINISHED'}


class AttractShotRelink(AttractShotFetchUpdate):
    bl_idname = "attract.shot_relink"
    bl_label = "Relink with Attract"

    strip_atc_object_id = bpy.props.StringProperty()

    @classmethod
    def poll(cls, context):
        strip = active_strip(context)
        return strip is not None and not getattr(strip, 'atc_object_id', None)

    def execute(self, context):
        strip = active_strip(context)

        status = self.relink(strip, self.strip_atc_object_id)
        if isinstance(status, set):
            return status

        strip.atc_object_id = self.strip_atc_object_id
        self.report({'INFO'}, "Shot {0} relinked".format(strip.atc_name))

        return {'FINISHED'}

    def invoke(self, context, event):
        maybe_id = context.window_manager.clipboard
        if len(maybe_id) == 24:
            try:
                int(maybe_id, 16)
            except ValueError:
                pass
            else:
                self.strip_atc_object_id = maybe_id

        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        layout = self.layout
        col = layout.column()
        col.prop(self, 'strip_atc_object_id', text='Shot ID')


class ATTRACT_OT_shot_open_in_browser(AttractOperatorMixin, Operator):
    bl_idname = 'attract.shot_open_in_browser'
    bl_label = 'Open in browser'
    bl_description = 'Opens a webbrowser to show the shot on Attract'

    def execute(self, context):
        from ..blender import PILLAR_WEB_SERVER_URL
        import webbrowser
        import urllib.parse

        strip = active_strip(context)

        url = urllib.parse.urljoin(PILLAR_WEB_SERVER_URL,
                                   'nodes/%s/redir' % strip.atc_object_id)
        webbrowser.open_new_tab(url)
        self.report({'INFO'}, 'Opened a browser at %s' % url)

        return {'FINISHED'}


class AttractShotDelete(AttractOperatorMixin, Operator):
    bl_idname = 'attract.shot_delete'
    bl_label = 'Delete Shot'
    bl_description = 'Remove this shot from Attract'

    confirm = bpy.props.BoolProperty(name='confirm')

    def execute(self, context):
        from .. import pillar

        if not self.confirm:
            self.report({'WARNING'}, 'Delete aborted.')
            return {'CANCELLED'}

        strip = active_strip(context)
        node = pillar.sync_call(Node.find, strip.atc_object_id)
        if not pillar.sync_call(node.delete):
            print('Unable to delete the strip node on Attract.')
            return {'CANCELLED'}

        remove_atc_props(strip)
        draw.tag_redraw_all_sequencer_editors()
        return {'FINISHED'}

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        layout = self.layout
        col = layout.column()
        col.prop(self, 'confirm', text="I hereby confirm I want to delete this shot.")


class AttractStripUnlink(AttractOperatorMixin, Operator):
    bl_idname = 'attract.strip_unlink'
    bl_label = 'Unlink shot from this strip'
    bl_description = 'Remove Attract props from the selected strip(s)'

    def execute(self, context):
        for strip in context.selected_sequences:
            atc_object_id = getattr(strip, 'atc_object_id')
            remove_atc_props(strip)

            if atc_object_id:
                self.report({'INFO'}, 'Shot %s has been unlinked from Attract.' % atc_object_id)

        draw.tag_redraw_all_sequencer_editors()
        return {'FINISHED'}


class AttractShotSubmitSelected(AttractOperatorMixin, Operator):
    bl_idname = 'attract.submit_selected'
    bl_label = 'Submit all selected'
    bl_description = 'Submits all selected strips to Attract'

    @classmethod
    def poll(cls, context):
        return bool(context.selected_sequences)

    def execute(self, context):
        # Check that the project is set up for Attract.
        node_type = self.find_node_type('attract_shot')
        if isinstance(node_type, set):
            return node_type

        for strip in context.selected_sequences:
            status = self.submit(strip)
            if isinstance(status, set):
                return status

        self.report({'INFO'}, 'All selected strips sent to Attract.')

        return {'FINISHED'}

    def submit(self, strip):
        atc_object_id = getattr(strip, 'atc_object_id', None)

        # Submit as new?
        if not atc_object_id:
            return self.submit_new_strip(strip)

        # Or just save to Attract.
        return self.submit_update(strip)


class ATTRACT_OT_open_meta_blendfile(AttractOperatorMixin, Operator):
    bl_idname = 'attract.open_meta_blendfile'
    bl_label = 'Open Blendfile'
    bl_description = 'Open Blendfile from movie strip metadata'

    @classmethod
    def poll(cls, context):
        return bool(any(cls.filename_from_metadata(s) for s in context.selected_sequences))

    @staticmethod
    def filename_from_metadata(strip):
        """Returns the blendfile name from the strip metadata, or None."""

        # Metadata is a dict like:
        # meta = {'END_FRAME': '88',
        #         'BLEND_FILE': 'metadata-test.blend',
        #         'SCENE': 'SüperSčene',
        #         'FRAME_STEP': '1',
        #         'START_FRAME': '32'}

        meta = strip.get('metadata', None)
        if not meta:
            return None

        return meta.get('BLEND_FILE', None) or None

    def execute(self, context):
        for strip in context.selected_sequences:
            meta = strip.get('metadata', None)
            if not meta:
                continue

            fname = meta.get('BLEND_FILE', None)
            if not fname: continue

            scene = meta.get('SCENE', None)
            self.open_in_new_blender(fname, scene)

        return {'FINISHED'}

    def open_in_new_blender(self, fname, scene):
        """
        :type fname: str
        :type scene: str
        """
        import subprocess
        import sys

        cmd = [
            bpy.app.binary_path,
            str(fname),
        ]

        cmd[1:1] = [v for v in sys.argv if v.startswith('--enable-')]

        if scene:
            cmd.extend(['--python-expr',
                        'import bpy; bpy.context.screen.scene = bpy.data.scenes["%s"]' % scene])
            cmd.extend(['--scene', scene])

        subprocess.Popen(cmd)


@contextlib.contextmanager
def thumbnail_render_settings(context, thumbnail_width=512):
    orig_res_x = context.scene.render.resolution_x
    orig_res_y = context.scene.render.resolution_y
    orig_percentage = context.scene.render.resolution_percentage
    orig_file_format = context.scene.render.image_settings.file_format
    orig_quality = context.scene.render.image_settings.quality

    try:
        # Update the render size to something thumbnaily.
        factor = orig_res_y / orig_res_x
        context.scene.render.resolution_x = thumbnail_width
        context.scene.render.resolution_y = round(thumbnail_width * factor)
        context.scene.render.resolution_percentage = 100
        context.scene.render.image_settings.file_format = 'JPEG'
        context.scene.render.image_settings.quality = 85

        yield
    finally:
        # Return the render settings to normal.
        context.scene.render.resolution_x = orig_res_x
        context.scene.render.resolution_y = orig_res_y
        context.scene.render.resolution_percentage = orig_percentage
        context.scene.render.image_settings.file_format = orig_file_format
        context.scene.render.image_settings.quality = orig_quality


class ATTRACT_OT_make_shot_thumbnail(AttractOperatorMixin,
                                     async_loop.AsyncModalOperatorMixin,
                                     Operator):
    bl_idname = 'attract.make_shot_thumbnail'
    bl_label = 'Render shot thumbnail'
    bl_description = 'Renders the current frame, and uploads it as thumbnail for the shot'

    stop_upon_exception = True

    async def async_execute(self, context):
        import tempfile

        # Later: for strip in context.selected_sequences:
        strip = active_strip(context)
        atc_object_id = getattr(strip, 'atc_object_id', None)
        if not atc_object_id:
            self.report({'ERROR'}, 'Strip %s not set up for Attract' % strip.name)
            self.quit()
            return

        with tempfile.NamedTemporaryFile() as tmpfile:
            with thumbnail_render_settings(context):
                bpy.ops.render.render()
            file_id = await self.upload_via_tempdir(bpy.data.images['Render Result'],
                                                    'attract_shot_thumbnail.jpg')

        if file_id is None:
            self.quit()
            return

        # Update the shot to include this file as the picture.
        node = pillarsdk.Node({'_id': atc_object_id})
        await pillar.pillar_call(
            node.patch,
            {
                'op': 'from-blender',
                '$set': {
                    'picture': file_id,
                }
            })

        self.report({'INFO'}, 'Thumbnail uploaded to Attract')
        self.quit()

    async def upload_via_tempdir(self, datablock, filename_on_cloud) -> pillarsdk.Node:
        """Saves the datablock to file, and uploads it to the cloud.

        Saving is done to a temporary directory, which is removed afterwards.

        Returns the node.
        """
        import tempfile
        import os.path

        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = os.path.join(tmpdir, filename_on_cloud)
            self.log.debug('Saving %s to %s', datablock, filepath)
            datablock.save_render(filepath)
            return await self.upload_file(filepath)

    async def upload_file(self, filename: str, fileobj=None):
        """Uploads a file to the cloud, attached to the image sharing node.

        Returns the node.
        """
        from .. import blender

        prefs = blender.preferences()
        project = self.find_project(prefs.attract_project.project)

        self.log.info('Uploading file %s', filename)
        resp = await pillar.pillar_call(
            pillarsdk.File.upload_to_project,
            project['_id'],
            'image/jpeg',
            filename,
            fileobj=fileobj)

        self.log.debug('Returned data: %s', resp)
        try:
            file_id = resp['file_id']
        except KeyError:
            self.log.error('Upload did not succeed, response: %s', resp)
            self.report({'ERROR'}, 'Unable to upload thumbnail to Attract: %s' % resp)
            return None

        self.log.info('Created file %s', file_id)
        self.report({'INFO'}, 'File succesfully uploaded to the cloud!')

        return file_id


def draw_strip_movie_meta(self, context):
    strip = active_strip(context)
    if not strip:
        return

    meta = strip.get('metadata', None)
    if not meta:
        return None

    box = self.layout.column(align=True)
    row = box.row(align=True)
    fname = meta.get('BLEND_FILE', None) or None
    if fname:
        row.label('Original Blendfile: %s' % fname)
        row.operator(ATTRACT_OT_open_meta_blendfile.bl_idname,
                     text='', icon='FILE_BLEND')
    sfra = meta.get('START_FRAME', '?')
    efra = meta.get('END_FRAME', '?')
    box.label('Original frame range: %s-%s' % (sfra, efra))


def register():
    bpy.types.Sequence.atc_is_synced = bpy.props.BoolProperty(name="Is synced")
    bpy.types.Sequence.atc_object_id = bpy.props.StringProperty(name="Attract Object ID")
    bpy.types.Sequence.atc_name = bpy.props.StringProperty(name="Shot Name")
    bpy.types.Sequence.atc_description = bpy.props.StringProperty(name="Shot description")
    bpy.types.Sequence.atc_notes = bpy.props.StringProperty(name="Shot notes")

    # TODO: get this from the project's node type definition.
    bpy.types.Sequence.atc_status = bpy.props.EnumProperty(
        items=[
            ('on_hold', 'On hold', 'The shot is on hold'),
            ('todo', 'Todo', 'Waiting'),
            ('in_progress', 'In progress', 'The show has been assigned'),
            ('review', 'Review', ''),
            ('final', 'Final', ''),
        ],
        name="Status")
    bpy.types.Sequence.atc_order = bpy.props.IntProperty(name="Order")

    bpy.types.SEQUENCER_PT_edit.append(draw_strip_movie_meta)

    bpy.utils.register_class(ToolsPanel)
    bpy.utils.register_class(AttractShotRelink)
    bpy.utils.register_class(AttractShotDelete)
    bpy.utils.register_class(AttractStripUnlink)
    bpy.utils.register_class(AttractShotFetchUpdate)
    bpy.utils.register_class(AttractShotSubmitSelected)
    bpy.utils.register_class(ATTRACT_OT_open_meta_blendfile)
    bpy.utils.register_class(ATTRACT_OT_shot_open_in_browser)
    bpy.utils.register_class(ATTRACT_OT_make_shot_thumbnail)
    draw.callback_enable()


def unregister():
    draw.callback_disable()
    bpy.utils.unregister_module(__name__)
    del bpy.types.Sequence.atc_is_synced
    del bpy.types.Sequence.atc_object_id
    del bpy.types.Sequence.atc_name
    del bpy.types.Sequence.atc_description
    del bpy.types.Sequence.atc_notes
    del bpy.types.Sequence.atc_status
    del bpy.types.Sequence.atc_order
