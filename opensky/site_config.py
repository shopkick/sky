# Copyright (c) Shopkick 2017
# See LICENSE for details.
import os

import yaml
import schema

from . import plugins
from . import config


def get_site_config(cache, logger, site_config_url):
    with logger.critical('site_config') as act:
        act['site_config'] = site_config_url
        if not site_config_url:
            return None
        src = config.GitRemoteRef.from_text(site_config_url)
        site_config_dir = cache.pull_project_git(
            'site_config', src.url, src.ref)
        # TODO: bring the filename out, possibly into a URL fragment
        default_config_fn = 'sky_site_config.yaml'
        site_config_path = os.path.join(site_config_dir, default_config_fn)
        ret = yaml.safe_load(open(site_config_path, 'rb'))
        ret = get_schema().validate(ret)
    return ret


def get_schema():
    '''
    get the schema for site-config
    '''
    global _CONFIG_SCHEMA
    if _CONFIG_SCHEMA is None:
        _CONFIG_SCHEMA = _build_schema()
    return _CONFIG_SCHEMA


_CONFIG_SCHEMA = None


def _build_schema():
    config_schema = {
        'pip': {
            'extra_pypi_urls': [str]
        },
        'services': {
            str: object
        },
        'service_groups': {
            str: object
        },
        'sk_custom': {
            'host_port_map': {str: [str]}
        },
    }
    site_config_plugins = plugins._SITE_CONFIG.collect()
    for plugin_builder in site_config_plugins.values():
        config_schema[schema.Optional(
            plugin_builder.sky_plugin.name)] = plugin_builder()
    # ignore_extra_keys allows for site-configs to be forwards compatible
    # as long as current keys aren't deleted or removed, new data
    # can safely be added without breaking the existing sky deployments
    return schema.Schema(config_schema, ignore_extra_keys=True)
