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

"""Blender-specific code.

Separated from __init__.py so that we can import & run from non-Blender environments.
"""

import logging
import os.path

import bpy
from bpy.types import AddonPreferences, Operator, WindowManager, Scene, PropertyGroup
from bpy.props import StringProperty, EnumProperty, PointerProperty, BoolProperty, IntProperty
import rna_prop_ui

from . import pillar, async_loop, flamenco
from .utils import pyside_cache, redraw

PILLAR_WEB_SERVER_URL = 'https://cloud.blender.org/'
# PILLAR_WEB_SERVER_URL = 'http://pillar-web:5001/'
PILLAR_SERVER_URL = '%sapi/' % PILLAR_WEB_SERVER_URL

ADDON_NAME = 'blender_cloud'
log = logging.getLogger(__name__)

icons = None


@pyside_cache('version')
def blender_syncable_versions(self, context):
    """Returns the list of items used by SyncStatusProperties.version EnumProperty."""

    bss = context.window_manager.blender_sync_status
    versions = bss.available_blender_versions
    if not versions:
        return [('', 'No settings stored in your Blender Cloud', '')]
    return [(v, v, '') for v in versions]


class SyncStatusProperties(PropertyGroup):
    status = EnumProperty(
        items=[
            ('NONE', 'NONE', 'We have done nothing at all yet.'),
            ('IDLE', 'IDLE', 'User requested something, which is done, and we are now idle.'),
            ('SYNCING', 'SYNCING', 'Synchronising with Blender Cloud.'),
        ],
        name='status',
        description='Current status of Blender Sync',
        update=redraw)

    version = EnumProperty(
        items=blender_syncable_versions,
        name='Version of Blender from which to pull',
        description='Version of Blender from which to pull')

    message = StringProperty(name='message', update=redraw)
    level = EnumProperty(
        items=[
            ('INFO', 'INFO', ''),
            ('WARNING', 'WARNING', ''),
            ('ERROR', 'ERROR', ''),
            ('SUBSCRIBE', 'SUBSCRIBE', ''),
        ],
        name='level',
        update=redraw)

    def report(self, level: set, message: str):
        assert len(level) == 1, 'level should be a set of one string, not %r' % level
        self.level = level.pop()
        self.message = message

        # Message can also be empty, just to erase it from the GUI.
        # No need to actually log those.
        if message:
            try:
                loglevel = logging._nameToLevel[self.level]
            except KeyError:
                loglevel = logging.WARNING
            log.log(loglevel, message)

    # List of syncable versions is stored in 'available_blender_versions' ID property,
    # because I don't know how to store a variable list of strings in a proper RNA property.
    @property
    def available_blender_versions(self) -> list:
        return self.get('available_blender_versions', [])

    @available_blender_versions.setter
    def available_blender_versions(self, new_versions):
        self['available_blender_versions'] = new_versions


@pyside_cache('project')
def bcloud_available_projects(self, context):
    """Returns the list of items used by BlenderCloudProjectGroup.project EnumProperty."""

    attr_proj = preferences().attract_project
    projs = attr_proj.available_projects
    if not projs:
        return [('', 'No projects available in your Blender Cloud', '')]
    return [(p['_id'], p['name'], '') for p in projs]


class BlenderCloudProjectGroup(PropertyGroup):
    status = EnumProperty(
        items=[
            ('NONE', 'NONE', 'We have done nothing at all yet'),
            ('IDLE', 'IDLE', 'User requested something, which is done, and we are now idle'),
            ('FETCHING', 'FETCHING', 'Fetching available projects from Blender Cloud'),
        ],
        name='status',
        update=redraw)

    project = EnumProperty(
        items=bcloud_available_projects,
        name='Cloud project',
        description='Which Blender Cloud project to work with')

    # List of projects is stored in 'available_projects' ID property,
    # because I don't know how to store a variable list of strings in a proper RNA property.
    @property
    def available_projects(self) -> list:
        return self.get('available_projects', [])

    @available_projects.setter
    def available_projects(self, new_projects):
        self['available_projects'] = new_projects


class BlenderCloudPreferences(AddonPreferences):
    bl_idname = ADDON_NAME

    # The following two properties are read-only to limit the scope of the
    # addon and allow for proper testing within this scope.
    pillar_server = StringProperty(
        name='Blender Cloud Server',
        description='URL of the Blender Cloud backend server',
        default=PILLAR_SERVER_URL,
        get=lambda self: PILLAR_SERVER_URL
    )

    local_texture_dir = StringProperty(
        name='Default Blender Cloud texture storage directory',
        subtype='DIR_PATH',
        default='//textures')

    open_browser_after_share = BoolProperty(
        name='Open browser after sharing file',
        description='When enabled, Blender will open a webbrowser',
        default=True
    )

    # TODO: store local path with the Attract project, so that people
    # can switch projects and the local path switches with it.
    attract_project = PointerProperty(type=BlenderCloudProjectGroup)
    attract_project_local_path = StringProperty(
        name='Local project path',
        description='Local path of your Attract project, used to search for blend files; '
                    'usually best to set to an absolute path',
        subtype='DIR_PATH',
        default='//../')

    flamenco_manager = PointerProperty(type=flamenco.FlamencoManagerGroup)
    # TODO: before making Flamenco public, change the defaults to something less Institute-specific.
    # NOTE: The assumption is that the workers can also find the files in the same path.
    #       This assumption is true for the Blender Institute.
    flamenco_job_file_path = StringProperty(
        name='Job file path',
        description='Path where to store job files, should be accesible for Workers too',
        subtype='DIR_PATH',
        default='/render/_flamenco/storage')

    # TODO: before making Flamenco public, change the defaults to something less Institute-specific.
    flamenco_job_output_path = StringProperty(
        name='Job output path',
        description='Path where to store output files, should be accessible for Workers',
        subtype='DIR_PATH',
        default='/render/_flamenco/output')
    flamenco_job_output_strip_components = IntProperty(
        name='Job output path strip components',
        description='The final output path comprises of the job output path, and the blend file '
                    'path relative to the project with this many path components stripped off '
                    'the front',
        min=0,
        default=0,
        soft_max=4,
    )

    def draw(self, context):
        import textwrap

        layout = self.layout

        # Carefully try and import the Blender ID addon
        try:
            import blender_id
        except ImportError:
            blender_id = None
            blender_id_profile = None
        else:
            blender_id_profile = blender_id.get_active_profile()

        if blender_id is None:
            msg_icon = 'ERROR'
            text = 'This add-on requires Blender ID'
            help_text = 'Make sure that the Blender ID add-on is installed and activated'
        elif not blender_id_profile:
            msg_icon = 'ERROR'
            text = 'You are logged out.'
            help_text = 'To login, go to the Blender ID add-on preferences.'
        elif bpy.app.debug and pillar.SUBCLIENT_ID not in blender_id_profile.subclients:
            msg_icon = 'QUESTION'
            text = 'No Blender Cloud credentials.'
            help_text = ('You are logged in on Blender ID, but your credentials have not '
                         'been synchronized with Blender Cloud yet. Press the Update '
                         'Credentials button.')
        else:
            msg_icon = 'WORLD_DATA'
            text = 'You are logged in as %s.' % blender_id_profile.username
            help_text = ('To logout or change profile, '
                         'go to the Blender ID add-on preferences.')

        # Authentication stuff
        auth_box = layout.box()
        auth_box.label(text=text, icon=msg_icon)

        help_lines = textwrap.wrap(help_text, 80)
        for line in help_lines:
            auth_box.label(text=line)
        if bpy.app.debug:
            auth_box.operator("pillar.credentials_update")

        # Texture browser stuff
        texture_box = layout.box()
        texture_box.enabled = msg_icon != 'ERROR'
        sub = texture_box.column()
        sub.label(text='Local directory for downloaded textures', icon_value=icon('CLOUD'))
        sub.prop(self, "local_texture_dir", text='Default')
        sub.prop(context.scene, "local_texture_dir", text='Current scene')

        # Blender Sync stuff
        bss = context.window_manager.blender_sync_status
        bsync_box = layout.box()
        bsync_box.enabled = msg_icon != 'ERROR'
        row = bsync_box.row().split(percentage=0.33)
        row.label('Blender Sync with Blender Cloud', icon_value=icon('CLOUD'))

        icon_for_level = {
            'INFO': 'NONE',
            'WARNING': 'INFO',
            'ERROR': 'ERROR',
            'SUBSCRIBE': 'ERROR',
        }
        msg_icon = icon_for_level[bss.level] if bss.message else 'NONE'
        message_container = row.row()
        message_container.label(bss.message, icon=msg_icon)

        sub = bsync_box.column()

        if bss.level == 'SUBSCRIBE':
            self.draw_subscribe_button(sub)
        self.draw_sync_buttons(sub, bss)

        # Image Share stuff
        share_box = layout.box()
        share_box.label('Image Sharing on Blender Cloud', icon_value=icon('CLOUD'))
        share_box.prop(self, 'open_browser_after_share')

        # Attract stuff
        attract_box = layout.box()
        self.draw_attract_buttons(attract_box, self.attract_project)

        # Flamenco stuff
        flamenco_box = layout.box()
        self.draw_flamenco_buttons(flamenco_box, self.flamenco_manager, context)

    def draw_subscribe_button(self, layout):
        layout.operator('pillar.subscribe', icon='WORLD')

    def draw_sync_buttons(self, layout, bss):
        layout.enabled = bss.status in {'NONE', 'IDLE'}

        buttons = layout.column()
        row_buttons = buttons.row().split(percentage=0.5)
        row_push = row_buttons.row()
        row_pull = row_buttons.row(align=True)

        row_push.operator('pillar.sync',
                          text='Save %i.%i settings' % bpy.app.version[:2],
                          icon='TRIA_UP').action = 'PUSH'

        versions = bss.available_blender_versions
        version = bss.version
        if bss.status in {'NONE', 'IDLE'}:
            if not versions or not version:
                row_pull.operator('pillar.sync',
                                  text='Find version to load',
                                  icon='TRIA_DOWN').action = 'REFRESH'
            else:
                props = row_pull.operator('pillar.sync',
                                          text='Load %s settings' % version,
                                          icon='TRIA_DOWN')
                props.action = 'PULL'
                props.blender_version = version
                row_pull.operator('pillar.sync',
                                  text='',
                                  icon='DOTSDOWN').action = 'SELECT'
        else:
            row_pull.label('Cloud Sync is running.')

    def draw_attract_buttons(self, attract_box, bcp: BlenderCloudProjectGroup):
        attract_row = attract_box.row(align=True)
        attract_row.label('Attract', icon_value=icon('CLOUD'))

        attract_row.enabled = bcp.status in {'NONE', 'IDLE'}
        row_buttons = attract_row.row(align=True)

        projects = bcp.available_projects
        project = bcp.project
        if bcp.status in {'NONE', 'IDLE'}:
            if not projects or not project:
                row_buttons.operator('pillar.projects',
                                     text='Find project to load',
                                     icon='FILE_REFRESH')
            else:
                row_buttons.prop(bcp, 'project')
                row_buttons.operator('pillar.projects',
                                     text='',
                                     icon='FILE_REFRESH')
        else:
            row_buttons.label('Fetching available projects.')

        attract_box.prop(self, 'attract_project_local_path')

    def draw_flamenco_buttons(self, flamenco_box, bcp: flamenco.FlamencoManagerGroup, context):
        flamenco_row = flamenco_box.row(align=True)
        flamenco_row.label('Flamenco', icon_value=icon('CLOUD'))

        flamenco_row.enabled = bcp.status in {'NONE', 'IDLE'}
        row_buttons = flamenco_row.row(align=True)

        if bcp.status in {'NONE', 'IDLE'}:
            if not bcp.available_managers or not bcp.manager:
                row_buttons.operator('flamenco.managers',
                                     text='Find Flamenco Managers',
                                     icon='FILE_REFRESH')
            else:
                row_buttons.prop(bcp, 'manager', text='Manager')
                row_buttons.operator('flamenco.managers',
                                     text='',
                                     icon='FILE_REFRESH')
        else:
            row_buttons.label('Fetching available managers.')

        path_box = flamenco_box.row(align=True)
        path_box.prop(self, 'flamenco_job_file_path')
        props = path_box.operator('flamenco.explore_file_path', text='', icon='DISK_DRIVE')
        props.path = self.flamenco_job_file_path

        job_output_box = flamenco_box.column(align=True)
        path_box = job_output_box.row(align=True)
        path_box.prop(self, 'flamenco_job_output_path')
        props = path_box.operator('flamenco.explore_file_path', text='', icon='DISK_DRIVE')
        props.path = self.flamenco_job_output_path

        job_output_box.prop(self, 'flamenco_job_output_strip_components',
                            text='Strip components')

        from .flamenco import render_output_path

        path_box = job_output_box.row(align=True)
        output_path = render_output_path(context)
        path_box.label(str(output_path))
        props = path_box.operator('flamenco.explore_file_path', text='', icon='DISK_DRIVE')
        props.path = str(output_path.parent)

        # TODO: make a reusable way to select projects, and use that for Attract and Flamenco.
        note_box = flamenco_box.column(align=True)
        note_box.label('NOTE: For now, Flamenco uses the same project as Attract.')
        note_box.label('This will change in a future version of the add-on.')


class PillarCredentialsUpdate(pillar.PillarOperatorMixin,
                              Operator):
    """Updates the Pillar URL and tests the new URL."""
    bl_idname = 'pillar.credentials_update'
    bl_label = 'Update credentials'
    bl_description = 'Resynchronises your Blender ID login with Blender Cloud'

    log = logging.getLogger('bpy.ops.%s' % bl_idname)

    @classmethod
    def poll(cls, context):
        # Only allow activation when the user is actually logged in.
        return cls.is_logged_in(context)

    @classmethod
    def is_logged_in(cls, context):
        try:
            import blender_id
        except ImportError:
            return False

        return blender_id.is_logged_in()

    def execute(self, context):
        import blender_id
        import asyncio

        # Only allow activation when the user is actually logged in.
        if not self.is_logged_in(context):
            self.report({'ERROR'}, 'No active profile found')
            return {'CANCELLED'}

        try:
            loop = asyncio.get_event_loop()
            loop.run_until_complete(self.check_credentials(context, set()))
        except blender_id.BlenderIdCommError as ex:
            log.exception('Error sending subclient-specific token to Blender ID')
            self.report({'ERROR'}, 'Failed to sync Blender ID to Blender Cloud')
            return {'CANCELLED'}
        except Exception as ex:
            log.exception('Error in test call to Pillar')
            self.report({'ERROR'}, 'Failed test connection to Blender Cloud')
            return {'CANCELLED'}

        self.report({'INFO'}, 'Blender Cloud credentials & endpoint URL updated.')
        return {'FINISHED'}


class PILLAR_OT_subscribe(Operator):
    """Opens a browser to subscribe the user to the Cloud."""
    bl_idname = 'pillar.subscribe'
    bl_label = 'Subscribe to the Cloud'
    bl_description = "Opens a page in a web browser to subscribe to the Blender Cloud"

    def execute(self, context):
        import webbrowser

        webbrowser.open_new_tab('https://cloud.blender.org/join')
        self.report({'INFO'}, 'We just started a browser for you.')

        return {'FINISHED'}


class PILLAR_OT_projects(async_loop.AsyncModalOperatorMixin,
                         pillar.AuthenticatedPillarOperatorMixin,
                         Operator):
    """Fetches the projects available to the user"""
    bl_idname = 'pillar.projects'
    bl_label = 'Fetch available projects'

    stop_upon_exception = True
    _log = logging.getLogger('bpy.ops.%s' % bl_idname)

    async def async_execute(self, context):
        if not await self.authenticate(context):
            return

        import pillarsdk
        from .pillar import pillar_call

        self.log.info('Going to fetch projects for user %s', self.user_id)

        preferences().attract_project.status = 'FETCHING'

        # Get all projects, except the home project.
        projects_user = await pillar_call(
            pillarsdk.Project.all,
            {'where': {'user': self.user_id,
                       'category': {'$ne': 'home'}},
             'sort': '-_created',
             'projection': {'_id': True,
                            'name': True},
             })

        projects_shared = await pillar_call(
            pillarsdk.Project.all,
            {'where': {'user': {'$ne': self.user_id},
                       'permissions.groups.group': {'$in': self.db_user.groups}},
             'sort': '-_created',
             'projection': {'_id': True,
                            'name': True},
             })

        # We need to convert to regular dicts before storing in ID properties.
        # Also don't store more properties than we need.
        projects = [{'_id': p['_id'], 'name': p['name']} for p in projects_user['_items']] + \
                   [{'_id': p['_id'], 'name': p['name']} for p in projects_shared['_items']]

        preferences().attract_project.available_projects = projects

        self.quit()

    def quit(self):
        preferences().attract_project.status = 'IDLE'
        super().quit()


class PILLAR_PT_image_custom_properties(rna_prop_ui.PropertyPanel, bpy.types.Panel):
    """Shows custom properties in the image editor."""

    bl_space_type = 'IMAGE_EDITOR'
    bl_region_type = 'UI'
    bl_label = 'Custom Properties'

    _context_path = 'edit_image'
    _property_type = bpy.types.Image


def preferences() -> BlenderCloudPreferences:
    return bpy.context.user_preferences.addons[ADDON_NAME].preferences


def load_custom_icons():
    global icons

    if icons is not None:
        # Already loaded
        return

    import bpy.utils.previews
    icons = bpy.utils.previews.new()
    my_icons_dir = os.path.join(os.path.dirname(__file__), 'icons')
    icons.load('CLOUD', os.path.join(my_icons_dir, 'icon-cloud.png'), 'IMAGE')


def unload_custom_icons():
    global icons

    if icons is None:
        # Already unloaded
        return

    bpy.utils.previews.remove(icons)
    icons = None


def icon(icon_name: str) -> int:
    """Returns the icon ID for the named icon.

    Use with layout.operator('pillar.image_share', icon_value=icon('CLOUD'))
    """

    return icons[icon_name].icon_id


def register():
    bpy.utils.register_class(BlenderCloudProjectGroup)
    bpy.utils.register_class(BlenderCloudPreferences)
    bpy.utils.register_class(PillarCredentialsUpdate)
    bpy.utils.register_class(SyncStatusProperties)
    bpy.utils.register_class(PILLAR_OT_subscribe)
    bpy.utils.register_class(PILLAR_OT_projects)
    bpy.utils.register_class(PILLAR_PT_image_custom_properties)

    addon_prefs = preferences()

    WindowManager.last_blender_cloud_location = StringProperty(
        name="Last Blender Cloud browser location",
        default="/")

    def default_if_empty(scene, context):
        """The scene's local_texture_dir, if empty, reverts to the addon prefs."""

        if not scene.local_texture_dir:
            scene.local_texture_dir = addon_prefs.local_texture_dir

    Scene.local_texture_dir = StringProperty(
        name='Blender Cloud texture storage directory for current scene',
        subtype='DIR_PATH',
        default=addon_prefs.local_texture_dir,
        update=default_if_empty)

    WindowManager.blender_sync_status = PointerProperty(type=SyncStatusProperties)

    load_custom_icons()


def unregister():
    unload_custom_icons()

    bpy.utils.unregister_class(BlenderCloudProjectGroup)
    bpy.utils.unregister_class(PillarCredentialsUpdate)
    bpy.utils.unregister_class(BlenderCloudPreferences)
    bpy.utils.unregister_class(SyncStatusProperties)
    bpy.utils.unregister_class(PILLAR_OT_subscribe)
    bpy.utils.unregister_class(PILLAR_OT_projects)
    bpy.utils.unregister_class(PILLAR_PT_image_custom_properties)

    del WindowManager.last_blender_cloud_location
    del WindowManager.blender_sync_status
