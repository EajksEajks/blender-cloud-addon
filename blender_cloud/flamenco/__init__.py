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

"""Flamenco interface.

The preferences are managed blender.py, the rest of the Flamenco-specific stuff is here.
"""
import logging

import bpy
from bpy.types import AddonPreferences, Operator, WindowManager, Scene, PropertyGroup
from bpy.props import StringProperty, EnumProperty, PointerProperty, BoolProperty, IntProperty

from .. import async_loop, pillar
from ..utils import pyside_cache, redraw

log = logging.getLogger(__name__)


@pyside_cache('manager')
def available_managers(self, context):
    """Returns the list of items used by a manager-selector EnumProperty."""

    from ..blender import preferences

    mngrs = preferences().flamenco_manager.available_managers
    if not mngrs:
        return [('', 'No managers available in your Blender Cloud', '')]
    return [(p['_id'], p['name'], '') for p in mngrs]


class FlamencoManagerGroup(PropertyGroup):
    manager = EnumProperty(
        items=available_managers,
        name='Flamenco Manager',
        description='Which Flamenco Manager to use for jobs')

    status = EnumProperty(
        items=[
            ('NONE', 'NONE', 'We have done nothing at all yet'),
            ('IDLE', 'IDLE', 'User requested something, which is done, and we are now idle'),
            ('FETCHING', 'FETCHING', 'Fetching available Flamenco managers from Blender Cloud'),
        ],
        name='status',
        update=redraw)

    # List of managers is stored in 'available_managers' ID property,
    # because I don't know how to store a variable list of strings in a proper RNA property.
    @property
    def available_managers(self) -> list:
        return self.get('available_managers', [])

    @available_managers.setter
    def available_managers(self, new_managers):
        self['available_managers'] = new_managers


class FLAMENCO_OT_fmanagers(async_loop.AsyncModalOperatorMixin,
                            pillar.AuthenticatedPillarOperatorMixin,
                            Operator):
    """Fetches the Flamenco Managers available to the user"""
    bl_idname = 'flamenco.managers'
    bl_label = 'Fetch available Flamenco Managers'

    stop_upon_exception = True
    _log = logging.getLogger('bpy.ops.%s' % bl_idname)

    @property
    def mypref(self) -> FlamencoManagerGroup:
        from ..blender import preferences

        return preferences().flamenco_manager

    async def async_execute(self, context):
        if not await self.authenticate(context):
            return

        from .sdk import Manager
        from ..pillar import pillar_call

        self.log.info('Going to fetch managers for user %s', self.user_id)

        self.mypref.status = 'FETCHING'
        managers = await pillar_call(Manager.all)

        # We need to convert to regular dicts before storing in ID properties.
        # Also don't store more properties than we need.
        as_list = [{'_id': p['_id'], 'name': p['name']} for p in managers['_items']]

        self.mypref.available_managers = as_list
        self.quit()

    def quit(self):
        self.mypref.status = 'IDLE'
        super().quit()


class FLAMENCO_OT_render(async_loop.AsyncModalOperatorMixin,
                         pillar.AuthenticatedPillarOperatorMixin,
                         Operator):
    """Performs a Blender render on Flamenco."""
    bl_idname = 'flamenco.render'
    bl_label = 'Render on Flamenco'

    stop_upon_exception = True
    _log = logging.getLogger('bpy.ops.%s' % bl_idname)

    async def async_execute(self, context):
        if not await self.authenticate(context):
            return

        import os.path
        from ..blender import preferences
        from pillarsdk import exceptions as sdk_exceptions

        prefs = preferences()
        scene = context.scene

        try:
            await create_job(self.user_id,
                             prefs.attract_project.project,
                             prefs.flamenco_manager.manager,
                             'blender-render',
                             {
                                 "blender_cmd": "{blender}",
                                 "chunk_size": scene.flamenco_render_chunk_size,
                                 "filepath": context.blend_data.filepath,
                                 "frames": scene.flamenco_render_frame_range
                             },
                             'Render %s' % os.path.basename(context.blend_data.filepath))
        except sdk_exceptions.ResourceInvalid as ex:
            self.report({'ERROR'}, 'Error creating Flamenco job: %s' % ex)
        else:
            self.report({'INFO'}, 'Flamenco job created.')
        self.quit()


class FLAMENCO_OT_scene_to_frame_range(Operator):
    """Sets the scene frame range as the Flamenco render frame range."""
    bl_idname = 'flamenco.scene_to_frame_range'
    bl_label = 'Sets the scene frame range as the Flamenco render frame range'

    def execute(self, context):
        s = context.scene
        s.flamenco_render_frame_range = '%i-%i' % (s.frame_start, s.frame_end)
        return {'FINISHED'}


async def create_job(user_id: str,
                     project_id: str,
                     manager_id: str,
                     job_type: str,
                     job_settings: dict,
                     job_name: str = None,
                     *,
                     job_description: str = None) -> str:
    """Creates a render job at Flamenco Server, returning the job ID."""

    import json
    from .sdk import Job
    from ..pillar import pillar_call

    job_attrs = {
        'status': 'queued',
        'priority': 50,
        'name': job_name,
        'settings': job_settings,
        'job_type': job_type,
        'user': user_id,
        'manager': manager_id,
        'project': project_id,
    }
    if job_description:
        job_attrs['description'] = job_description

    log.info('Going to create Flamenco job:\n%s',
             json.dumps(job_attrs, indent=4, sort_keys=True))

    job = Job(job_attrs)
    await pillar_call(job.create)

    log.info('Job created succesfully: %s', job._id)
    return job._id


def draw_render_button(self, context):
    layout = self.layout

    from ..blender import icon

    flamenco_box = layout.box()
    flamenco_box.label('Flamenco', icon_value=icon('CLOUD'))
    flamenco_box.prop(context.scene, 'flamenco_render_chunk_size')

    frange_row = flamenco_box.row(align=True)
    frange_row.prop(context.scene, 'flamenco_render_frame_range')
    frange_row.operator('flamenco.scene_to_frame_range', text='', icon='ARROW_LEFTRIGHT')

    flamenco_box.operator('flamenco.render', text='Render on Flamenco', icon='RENDER_ANIMATION')


def register():
    bpy.utils.register_class(FlamencoManagerGroup)
    bpy.utils.register_class(FLAMENCO_OT_fmanagers)
    bpy.utils.register_class(FLAMENCO_OT_render)
    bpy.utils.register_class(FLAMENCO_OT_scene_to_frame_range)

    scene = bpy.types.Scene
    scene.flamenco_render_chunk_size = IntProperty(
        name='Chunk size',
        description='Maximum number of frames to render per task',
        default=10,
    )
    scene.flamenco_render_frame_range = StringProperty(
        name='Frame range',
        description='Frames to render, in "printer range" notation'
    )

    bpy.types.RENDER_PT_render.append(draw_render_button)


def unregister():
    bpy.types.RENDER_PT_render.remove(draw_render_button)
    bpy.utils.unregister_module(__name__)

    wm = bpy.types.WindowManager
    del wm.flamenco_render_chunk_size
