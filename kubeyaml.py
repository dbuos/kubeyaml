import sys
import argparse
import functools
import collections
import re
from ruamel.yaml import YAML

# The container name, by proclamation, used for an image supplied in a
# FluxHelmRelease
FHR_CONTAINER = 'chart-image'

class NotFound(Exception):
    pass

class UnresolvablePath(Exception):
    pass

def parse_args():
    p = argparse.ArgumentParser()
    subparsers = p.add_subparsers()

    image = subparsers.add_parser('image', help='update an image ref')
    image.add_argument('--namespace', required=True)
    image.add_argument('--kind', required=True)
    image.add_argument('--name', required=True)
    image.add_argument('--container', required=True)
    image.add_argument('--image', required=True)
    image.set_defaults(func=update_image)

    def keyValuePair(s):
        k, v = s.split('=')
        return k, v

    annotation = subparsers.add_parser('annotate', help='update annotations')
    annotation.add_argument('--namespace', required=True)
    annotation.add_argument('--kind', required=True)
    annotation.add_argument('--name', required=True)
    annotation.add_argument('notes', nargs='+', type=keyValuePair)
    annotation.set_defaults(func=update_annotations)

    set = subparsers.add_parser('set', help='update values by their dot notation paths')
    set.add_argument('--namespace', required=True)
    set.add_argument('--kind', required=True)
    set.add_argument('--name', required=True)
    set.add_argument('paths', nargs='+', type=keyValuePair)
    set.set_defaults(func=set_paths)

    return p.parse_args()

def yaml():
    y = YAML()
    y.explicit_start = True
    y.explicit_end = False
    y.preserve_quotes = True
    return y

def bail(reason):
        sys.stderr.write(reason); sys.stderr.write('\n')
        sys.exit(2)

class AlwaysFalse(object):
    def __init__(self):
        pass

    def __get__(self, instance, owner):
        return False

    def __set__(self, instance, value):
        pass

def apply_to_yaml(fn, infile, outfile):
    # fn :: iterator a -> iterator b
    y = yaml()
    # Hack to make sure no end-of-document ("...") is ever added
    y.Emitter.open_ended = AlwaysFalse()
    docs = y.load_all(infile)
    y.dump_all(fn(docs), outfile)

def update_image(args, docs):
    """Update the manifest specified by args, in the stream of docs"""
    found = False
    for doc in docs:
        if not found:
            for m in manifests(doc):
                c = find_container(args, m)
                if c != None:
                    set_container_image(m, c, args.image)
                    found = True
                    break
        yield doc
    if not found:
        raise NotFound()

def update_annotations(spec, docs):
    def ensure(d, *keys):
        for k in keys:
            try:
                d = d[k]
            except KeyError:
                d[k] = dict()
                d = d[k]
        return d

    found = False
    for doc in docs:
        if not found:
            for m in manifests(doc):
                if match_manifest(spec, m):
                    notes = ensure(m, 'metadata', 'annotations')
                    for k, v in spec.notes:
                        if v == '':
                            try:
                                del notes[k]
                            except KeyError:
                                pass
                        else:
                            notes[k] = v
                    if len(notes) == 0:
                        del m['metadata']['annotations']
                    found = True
                    break
        yield doc
    if not found:
        raise NotFound()

def set_paths(spec, docs):
    def set_path(d, path, value):
        keys = path.split(".")
        for k in keys[:-1]:
            if k not in d:
                return False
            d = d[k]
        if isinstance(d[keys[-1]], collections.Mapping):
            return False
        d[keys[-1]] = value
        return True

    found = False
    unresolvable = list()
    for doc in docs:
        if not found:
            for m in manifests(doc):
                if match_manifest(spec, m):
                    for k, v in spec.paths:
                        if not set_path(m, k, v):
                            unresolvable.append(k)
                    found = True
                    break
        yield doc
    if len(unresolvable):
        raise UnresolvablePath(unresolvable)
    if not found:
        raise NotFound()

def manifests(doc):
    if doc == None:
        return collections.OrderedDict()
    if doc['kind'].endswith('List'):
        for m in doc['items']:
            yield m
    else:
        yield doc

def match_manifest(spec, manifest):
    try:
        # NB treat the Kind as case-insensitive
        if manifest['kind'].lower() != spec.kind.lower():
            return False
        if manifest['metadata'].get('namespace', 'default') != spec.namespace:
            return False
        if manifest['metadata']['name'] != spec.name:
            return False
    except KeyError:
        return False
    return True

def podspec(manifest):
    if manifest['kind'] == 'CronJob':
        spec = manifest['spec']['jobTemplate']['spec']['template']['spec']
    else:
        spec = manifest['spec']['template']['spec']
    return spec

def containers(manifest):
    if manifest['kind'] in ['FluxHelmRelease', 'HelmRelease']:
        return fluxhelmrelease_containers(manifest)
    spec = podspec(manifest)
    return spec.get('containers', []) + spec.get('initContainers', [])

def find_container(spec, manifest):
    if not match_manifest(spec, manifest):
        return None
    for c in containers(manifest):
        if c['name'] == spec.container:
            return c
    return None

def set_container_image(manifest, container, image):
    if manifest['kind'] in ['FluxHelmRelease', 'HelmRelease']:
        set_fluxhelmrelease_container(manifest, container, image)
    else:
        container['image'] = image

def mappings(values):
    return ((k, values[k]) for k in values if isinstance(values[k], collections.Mapping))

# There are different ways of interpreting FluxHelmRelease values as
# images, and we have to sniff to see which to use.
def fluxhelmrelease_containers(manifest):
    def get_image(values):
        image = values['image']
        if isinstance(image, collections.Mapping) and 'repository' in image:
            values = image
            image = image['repository']
        if 'registry' in values and values['registry'] != '':
            image = '%s/%s' % (values['registry'], image)
        if 'tag' in values and values['tag'] != '':
            image = '%s:%s' % (image, values['tag'])
        return image

    containers = []
    values = manifest['spec']['values']
    # Easiest one: the values section has a key called `image`, which
    # has the image used somewhere in the templates. Since we don't
    # know which container it appears in, it gets a standard name.
    if 'image' in values:
        containers =  [{
            'name': FHR_CONTAINER,
            'image': get_image(values),
        }]
    # Second easiest: if there's at least one dict in values that has
    # a key `image`, then all such dicts are treated as containers,
    # named for their key.
    for k, v in mappings(values):
        if 'image' in v:
            containers.append({'name': k, 'image': get_image(v)})
    return containers

def set_fluxhelmrelease_container(manifest, container, replace):
    # The logic within this method (almost) equals:
    # https://github.com/weaveworks/flux/blob/5b15a94397d58b69a2daedae3bcc377e4901435b/image/image.go#L136
    def parse_ref():
        reg, im, tag = '', '', ''
        try:
            segments = replace.split('/')
            if len(segments) == 1:
                im = replace
            elif len(segments) == 2:
                domainComponent = '([a-zA-Z0-9]|[a-zA-Z0-9][a-zA-Z0-9-]*[a-zA-Z0-9])'
                domain = '(localhost|(%s([.]%s)+))(:[0-9]+)?' % (domainComponent, domainComponent)
                if re.fullmatch(domain, segments[0]):
                    reg = segments[0]
                    im = segments[1]
                else:
                    im = replace
            else:
                reg = segments[0]
                im = '/'.join(segments[1:])

            segments = im.split(':')
            if len(segments) == 2:
                im, tag = segments
            elif len(segments) == 3:
                im = ':'.join(segments[:2])
                tag = segments[2]
        except ValueError:
            pass
        return reg, im, tag

    def set_image(values):
        image = values['image']
        imageKey = 'image'

        if isinstance(image, collections.Mapping) and 'repository' in image:
            values = image
            imageKey = 'repository'

        reg, im, tag = parse_ref()

        if 'registry' in values and 'tag' in values:
            values['registry'] = reg
            values[imageKey] = im
            values['tag'] = tag
        elif 'registry' in values:
            values['registry'] = reg
            values[imageKey] = ':'.join(filter(None, [im, tag]))
        elif 'tag' in values:
            values[imageKey] = '/'.join(filter(None, [reg, im]))
            values['tag'] = tag
        else:
            values[imageKey] = replace

    values = manifest['spec']['values']
    if container['name'] == FHR_CONTAINER and 'image' in values:
        set_image(values)
        return
    for k, v in mappings(values):
        if k == container['name'] and 'image' in v:
            set_image(v)
            return
    raise NotFound

def main():
    args = parse_args()
    try:
        apply_to_yaml(functools.partial(args.func, args), sys.stdin, sys.stdout)
    except NotFound:
        bail("manifest not found")
    except UnresolvablePath as e:
        bail("unable to resolve path(s):\n" + '\n'.join(e.args[0]))

if __name__ == "__main__":
    main()
