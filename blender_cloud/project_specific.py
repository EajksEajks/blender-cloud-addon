"""Handle saving and loading project-specific settings."""

import contextlib
import logging

# Names of BlenderCloudPreferences properties that are both project-specific
# and simple enough to store directly in a dict.
PROJECT_SPECIFIC_SIMPLE_PROPS = (
    'cloud_project_local_path',
)

log = logging.getLogger(__name__)
project_settings_loading = False


@contextlib.contextmanager
def mark_as_loading():
    """Sets project_settings_loading=True while the context is active."""
    global project_settings_loading
    project_settings_loading = True
    try:
        yield
    finally:
        project_settings_loading = False


def handle_project_update(_=None, _2=None):
    """Handles changing projects, which may cause extensions to be disabled/enabled.

    Ignores arguments so that it can be used as property update callback.
    """

    from .blender import preferences, project_extensions

    with mark_as_loading():
        prefs = preferences()
        project_id = prefs.project.project
        log.info('Updating internal state to reflect extensions enabled on current project %s.',
                 project_id)

        project_extensions.cache_clear()

        from blender_cloud import attract, flamenco
        attract.deactivate()
        flamenco.deactivate()

        enabled_for = project_extensions(project_id)
        log.info('Project extensions: %s', enabled_for)
        if 'attract' in enabled_for:
            attract.activate()
        if 'flamenco' in enabled_for:
            flamenco.activate()

        # Load project-specific settings from the last time we visited this project.
        ps = prefs.get('project_settings', {}).get(project_id, {})
        if not ps:
            log.debug('no project-specific settings are available, '
                      'only resetting available Flamenco Managers')
            # The Flamenco Manager should really be chosen explicitly out of the available
            # Managers.
            prefs.flamenco_manager.available_managers = []
            return

        if log.isEnabledFor(logging.DEBUG):
            from pprint import pformat
            log.debug('loading project-specific settings:\n%s', pformat(ps.to_dict()))

        # Restore simple properties.
        for name in PROJECT_SPECIFIC_SIMPLE_PROPS:
            if name in ps and hasattr(prefs, name):
                setattr(prefs, name, ps[name])

        # Restore Flamenco settings.
        prefs.flamenco_manager.available_managers = ps.get('flamenco_available_managers', [])
        flamenco_manager_id = ps.get('flamenco_manager_id')
        if flamenco_manager_id:
            log.debug('setting flamenco manager to %s', flamenco_manager_id)
            try:
                prefs.flamenco_manager.manager = flamenco_manager_id
            except TypeError:
                log.warning('manager %s for this project could not be found', flamenco_manager_id)
            else:
                # Load per-project, per-manager settings for the current Manager.
                try:
                    pppm = ps['flamenco_managers_settings'][flamenco_manager_id]
                except KeyError:
                    # No settings for this manager, so nothing to do.
                    pass
                else:
                    prefs.flamenco_job_file_path = pppm['file_path']
                    prefs.flamenco_job_output_path = pppm['output_path']
                    prefs.flamenco_job_output_strip_components = pppm['output_strip_components']
        else:
            log.debug('Resetting Flamenco Manager to None')
            prefs.flamenco_manager.manager = None


def store(_=None, _2=None):
    """Remember project-specific settings as soon as one of them changes.

    Ignores arguments so that it can be used as property update callback.

    No-op when project_settings_loading=True, to prevent saving project-
    specific settings while they are actually being loaded.
    """
    from .blender import preferences

    global project_settings_loading
    if project_settings_loading:
        return

    prefs = preferences()
    project_id = prefs.project.project
    all_settings = prefs.get('project_settings', {})
    ps = all_settings.get(project_id, {})  # either a dict or bpy.types.IDPropertyGroup

    for name in PROJECT_SPECIFIC_SIMPLE_PROPS:
        ps[name] = getattr(prefs, name)

    # Store project-specific Flamenco settings
    ps['flamenco_manager_id'] = prefs.flamenco_manager.manager
    ps['flamenco_available_managers'] = prefs.flamenco_manager.available_managers

    # Store per-project, per-manager settings for the current Manager.
    pppm = ps.get('flamenco_managers_settings', {})
    pppm[prefs.flamenco_manager.manager] = {
        'file_path': prefs.flamenco_job_file_path,
        'output_path': prefs.flamenco_job_output_path,
        'output_strip_components': prefs.flamenco_job_output_strip_components}
    ps['flamenco_managers_settings'] = pppm  # IDPropertyGroup has no setdefault() method.

    # Store this project's settings in the preferences.
    all_settings[project_id] = ps
    prefs['project_settings'] = all_settings

    if log.isEnabledFor(logging.DEBUG):
        from pprint import pformat
        if hasattr(all_settings, 'to_dict'):
            to_log = all_settings.to_dict()
        else:
            to_log = all_settings
        log.debug('Saving project-specific settings:\n%s', pformat(to_log))
