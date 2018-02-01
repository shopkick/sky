# Copyright (c) Shopkick 2017
# See LICENSE for details.
import sys
import json
import socket
import argparse

from boltons.tableutils import Table

from opensky import plugins, docker, shell


class ServiceNotFound(ValueError):
    pass


class InvalidPort(ValueError):
    pass


def json_dumps(obj, pretty=True):
    if pretty:
        return json.dumps(obj, indent=2, sort_keys=True)
    else:
        return json.dumps(obj, sort_keys=True)


@plugins.register_command(
    'services',
    requires=('site_config', 'docker_runner'),
    help='interact with and inspect one or more sky-configured services')
def services_plugin(argv, reqs):
    prs = get_argparser()
    args = prs.parse_args(argv[1:])
    return args.func(args, reqs)


def get_argparser():
    if sys.platform.startswith('linux'):
        plat = 'linux'
    elif sys.platform == 'darwin':
        plat = 'darwin'
    else:
        raise RuntimeError('sky only supports Linux and MacOS (darwin), not %r'
                           % sys.platform)

    prs = argparse.ArgumentParser(prog='services')
    prs.set_defaults(cur_platform=plat)

    subprs = prs.add_subparsers(dest='cmd')
    # http://bugs.python.org/issue9253
    # http://stackoverflow.com/a/18283730/1599393
    subprs.required = True

    desc = 'start one or more services, as configured in the site config'
    start_prs = subprs.add_parser('start', description=desc)
    add_arg = start_prs.add_argument
    add_arg('services', nargs='+',
            help='one or more service_names or aliases to start')
    start_prs.set_defaults(func=start_services)

    desc = 'check all services, as configured in the site config'
    check_prs = subprs.add_parser('check', description=desc)
    add_arg = check_prs.add_argument
    add_arg('--json', action='store_true', help='output structured json')
    add_arg('services', nargs='*',
            help='one or more service_names or aliases to start')
    check_prs.set_defaults(func=check_services)

    return prs


def start_services(args, reqs):
    service_names = args.services

    res_images, unres_images = _resolve_images(reqs.site_config,
                                               service_names)

    if unres_images:
        raise ServiceNotFound('unrecognized services: %r'
                              ' (see site config for more info)' % unres_images)

    composition = docker.Dockercompose('start_services', res_images)
    reqs.docker_runner.run_composition(composition, 'start_services')


def check_services(args, reqs):
    # TODO: support args for filtering by services
    site_config = reqs.site_config
    service_map = site_config['services']
    res = {}
    images, _ = _resolve_images(site_config, service_map.keys())
    for image in images:
        ports = [int(p.partition(':')[0]) for p in image.ports]
        ext_port_map = dict([(int(p.partition(':')[0]), int(p.partition(':')[2]))
                             for p in image.ports])
        res[image.name] = {'ports':{}, 'container_id': None, 'status': 'down'}
        if not ports:
            continue
        for port in ports:
            res[image.name]['ports'][port] = _is_port_open(port)
        primary_port = ports[0]
        is_up = res[image.name]['ports'][primary_port]
        if is_up:
            res[image.name]['status'] = 'up'
            try:
                cid = reqs.docker_runner.get_port_container_id(ext_port_map[primary_port])
            except shell.ShellSubprocessError:
                # TODO: remove this once we safely depend on docker 17.06+
                cid = None
            res[image.name]['container_id'] = cid

    if args.json:
        print json_dumps(res)
    else:
        print table_dumps(res)
    return


def table_dumps(res):
    tab = Table(headers=['   name   ', ' port ', 'status', 'container'])
    rows = []
    for service_name, v in res.items():
        rows.append([service_name, str(v['ports'].keys()[0] if v['ports'] else ''),
                     v['status'], v['container_id']])
    rows.sort(key=lambda r: r[0])
    tab.extend(rows)
    return tab.to_text()


def _is_port_open(port, host='127.0.0.1'):
    port = int(port)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    res = sock.connect_ex((host, port))
    sock.close()
    return res == 0


def _resolve_images(site_config, image_names):
    service_map = site_config['services']

    res_images = []
    unres_images = []
    for name in image_names:
        try:
            cur_image = service_map[name]
        except KeyError:
            unres_images.append(name)
            continue

        # TODO: sky_image vs docker_image

        kw = dict(cur_image)
        kw['name'] = name
        if 'docker_image' in kw:
            kw['image'] = kw.pop('docker_image')
        elif 'sky_image' in kw:
            kw['image'] = kw.pop('sky_image')
        if kw.get('ports') is not None:
            kw['ports'] = _parse_image_spec_ports(kw['ports'])
        cur_service = docker.ImageService(**kw)
        res_images.append(cur_service)
    return res_images, unres_images


def _parse_image_spec_ports(ports):
    ret = []
    if not ports:
        return ret
    for p in ports:
        if isinstance(p, int):
            p = '%s:%s' % (p, p)
        elif not isinstance(p, (unicode, bytes)):
            raise InvalidPort('expected int or unicode for port, not %r' % p)
        ret.append(p)
    return ret
