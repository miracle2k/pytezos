import pendulum
import re
from datetime import datetime
from os.path import join, dirname
from decimal import Decimal
from collections import namedtuple, defaultdict

from pytezos.encoding import parse_address, parse_public_key

Nested = namedtuple('Nested', ['prim', 'args'])
Schema = namedtuple('Schema', ['metadata', 'bin_types', 'bin_to_json', 'json_types', 'json_to_bin'])

meaningful_types = ['key', 'key_hash', 'signature', 'timestamp', 'address']
first_cap_re = re.compile('(.)([A-Z][a-z]+)')
all_cap_re = re.compile('([a-z0-9])([A-Z])')


def to_snake_case(name):
    s1 = first_cap_re.sub(r'\1_\2', name)
    return all_cap_re.sub(r'\1_\2', s1).lower()


def decode_literal(node, prim):
    core_type, value = next(iter(node.items()))
    if prim in ['int', 'nat']:
        return int(value)
    if prim == 'timestamp':
        if core_type == 'int':
            dt = pendulum.from_timestamp(int(value))
        else:
            dt = pendulum.parse(value)
        return dt.strftime('%Y-%m-%dT%H:%M:%SZ')
    if prim == 'mutez':
        return Decimal(value) / 10 ** 6
    if prim == 'bool':
        return value == 'True'
    if core_type == 'bytes':
        if prim in ['address', 'key_hash', 'contract']:
            return parse_address(bytes.fromhex(value))
        if prim == 'key':
            return parse_public_key(bytes.fromhex(value))
    return value


def encode_literal(value, prim):
    if prim in ['int', 'nat']:
        core_type = 'int'
        value = str(value)
    elif prim == 'timestamp':
        core_type = 'string'
        if isinstance(value, int):
            value = pendulum.from_timestamp(value)
        if isinstance(value, pendulum.DateTime) or isinstance(value, datetime):
            value = value.strftime('%Y-%m-%dT%H:%M:%SZ')
    elif prim == 'mutez':
        core_type = 'int'
        if isinstance(value, Decimal):
            value = int(value * 10 ** 6)
        if isinstance(value, int):
            value = str(value)
    elif prim == 'bool':
        core_type = 'prim'
        value = 'True' if value else 'False'
    elif prim == 'bytes':
        core_type = 'bytes'
    else:
        core_type = 'string'
        value = str(value)

    return {core_type: value}


def get_flat_nested(nested: Nested):
    flat_args = list()
    for arg in nested.args:
        if isinstance(arg, Nested) and arg.prim == nested.prim:
            flat_args.extend(get_flat_nested(arg))
        else:
            flat_args.append(arg)
    return flat_args


def make_dict(**kwargs) -> dict:
    return {k: v for k, v in kwargs.items() if v}


def collapse_micheline(code) -> dict:
    metadata = dict()

    def get_annotation(x, prefix, default=None):
        return next((a[1:] for a in x.get('annots', []) if a[0] == prefix), default)

    def parse_node(node, path='0', parent_prim=None, entry=None):
        if node['prim'] in ['storage', 'parameter']:
            return parse_node(node['args'][0])

        fieldname = get_annotation(node, '%')
        typename = get_annotation(node, ':')

        metadata[path] = make_dict(
            prim=node['prim'],
            typename=typename,
            fieldname=fieldname,
            entry=entry
        )

        if node['prim'] == 'option':
            return parse_node(
                node=node['args'][0],
                path=path + '0',
                parent_prim=parent_prim,
                entry=fieldname
            )

        args = [
            parse_node(arg, path=path + str(i), parent_prim=node['prim'])
            for i, arg in enumerate(node.get('args', []))
        ]

        if node['prim'] in ['pair', 'or']:
            res = Nested(node['prim'], args)
            if typename or parent_prim != node['prim']:
                args = get_flat_nested(res)
            else:
                return res

        if args:
            metadata[path]['args'] = list(map(lambda x: x['path'], args))

        return dict(path=path, args=args)

    parse_node(code)
    return metadata


def build_maps(metadata: dict):
    bin_types = {k: v['prim'] for k, v in metadata.items()}
    bin_to_json, json_types, json_to_bin = {}, {}, {}

    def get_entry(bin_path):
        node = metadata[bin_path]
        entry = node.get('entry', node.get('fieldname', node.get('typename')))
        return to_snake_case(entry.replace('_Liq_entry_', '')) if entry else None

    def get_lr_path(bin_path):
        entry = ''
        for i in range(len(bin_path) - 1, 0, -1):
            lpath = bin_path[:i]
            if bin_types[lpath] == 'or':
                entry = {'0': 'l', '1': 'r'}[bin_path[i]] + entry
            else:
                break
        return entry

    def get_key(bin_path):
        node = metadata[bin_path]
        default = node['prim'] if node['prim'] in meaningful_types else None
        key = node.get('typename', node.get('fieldname', default))
        return to_snake_case(key) if key else None

    def parse_node(bin_path='0', json_path='/'):
        node = metadata[bin_path]

        if node['prim'] in ['list', 'set']:
            json_types[json_path] = 'list'
            parse_node(node['args'][0], join(json_path, '{}'))

        elif node['prim'] in ['map', 'big_map']:
            json_types[json_path] = 'dict'
            parse_node(node['args'][1], join(json_path, '{}'))

        elif node['prim'] in ['pair', 'or']:
            keys = list(map(get_key if node['prim'] == 'pair' else get_entry, node['args']))
            named = all(keys) and len(keys) == len(set(keys))

            if not named and node['prim'] == 'or':
                keys = list(map(get_lr_path, node['args']))
                named = True

            json_types[json_path] = 'dict' if named else 'list'
            for i, arg in enumerate(node['args']):
                parse_node(arg, join(json_path, keys[i] if named else str(i)))

        bin_to_json[bin_path], json_to_bin[json_path] = json_path, bin_path

    parse_node()
    return bin_types, bin_to_json, json_types, json_to_bin


def parse_micheline(data, bin_to_json: dict, bin_types: dict, root='0'):
    json_values = dict()
    json_root = bin_to_json[root]

    def get_json_path(bin_path, params: list):
        template = bin_to_json[bin_path]
        if json_root != '/' and template.startswith(json_root) and template != json_root:
            template = template[len(json_root):]
        return template.format(*params)

    def is_map(sequence):
        return all(map(lambda x: isinstance(x, dict) and x.get('prim') == 'Elt', sequence))

    def parse_node(node, bin_path, params):
        if isinstance(node, dict):
            if node.get('prim') in ['Left', 'Some']:
                parse_node(node['args'][0], bin_path + '0', params)
            elif node.get('prim') == 'Right':
                parse_node(node['args'][0], bin_path + '1', params)
            elif node.get('prim') == 'Pair':
                for i, arg in enumerate(node['args']):
                    parse_node(arg, bin_path + str(i), params)
            elif node.get('prim') == 'Elt':
                assert False  # should be already handled
            else:
                if node.get('prim') == 'None':
                    value = None
                    bin_path += '0'
                else:
                    value = decode_literal(node, bin_types[bin_path])

                json_path = get_json_path(bin_path, params)
                json_values[json_path] = value

        elif isinstance(node, list):
            if is_map(node):
                for elt in node:
                    key = next(iter(elt['args'][0].values()))
                    parse_node(elt['args'][1], bin_path + '1', params + [key])
            else:
                for i, arg in enumerate(node):
                    parse_node(arg, bin_path + '0', params + [i])
        else:
            raise ValueError(node, bin_path)

    parse_node(data, root, [])
    return json_values


def make_json(json_values: dict, json_types: dict, root='/'):

    def make_container(path):
        path = join(root, path)
        alt_path = join(dirname(path), '{}')
        json_type = json_types.get(path, json_types.get(alt_path))
        if json_type == 'dict':
            return {}
        elif json_type == 'list':
            return []
        else:
            raise KeyError(path)

    tree = make_container('/')

    for json_path, value in json_values.items():
        node = tree
        lpath = '/'

        for key in json_path.lstrip('/').split('/'):
            lpath = join(lpath, key)
            if key.isdigit():
                key = int(key)
            try:
                node = node[key]
            except (KeyError, IndexError):
                if isinstance(node, list):
                    node.extend([None] * (key - len(node) + 1))
                if lpath == json_path:
                    node[key] = value
                else:
                    node[key] = make_container(lpath)
                    node = node[key]

    return tree


def parse_json(data, json_to_bin: dict, bin_types: dict, root='/'):
    bin_values = defaultdict(dict)

    def parse_entry(bin_path, index):
        for i in range(len(bin_path) - 1, 0, -1):
            lpath = bin_path[:i]
            if bin_types[lpath] == 'or':
                bin_values[lpath][index] = bin_path[i]

    def parse_node(node, json_path, index='0'):
        bin_path = json_to_bin[json_path]
        prim = bin_types[bin_path]

        if isinstance(node, dict):
            if prim in ['map', 'big_map']:
                bin_values[bin_path][index] = len(node)
                for i, (key, value) in enumerate(node.items()):
                    bin_values[bin_path + '0'][f'{index}:{i}'] = key
                    parse_node(value, join(json_path, '{}'), f'{index}:{i}')

            elif prim in ['pair', 'or']:
                for key, value in node.items():
                    parse_node(value, join(json_path, key), index)
            else:
                raise ValueError(node, json_path)

        elif isinstance(node, list):
            if prim in ['list', 'set']:
                bin_values[bin_path][index] = len(node)
                for i, value in enumerate(node):
                    parse_node(value, join(json_path, '{}'), f'{index}:{i}')

            elif prim == 'or':
                assert len(node) == 1
                parse_node(node[0], join(json_path, '0'), index)

            elif prim == 'pair':
                for i, value in enumerate(node):
                    parse_node(value, join(json_path, str(i)), index)
        else:
            if prim == 'or':  # enum
                parse_entry(json_to_bin[join(json_path, str(node))], index)
            else:
                bin_values[bin_path][index] = node
                parse_entry(bin_path, index)

    parse_node(data, root)
    return dict(bin_values)


def make_micheline(bin_values: dict, bin_types: dict, root='0'):

    def get_length(bin_path, index):
        try:
            length = bin_values[bin_path][index]
        except KeyError:
            length = 0  # TODO: make sure there is an option ahead
        return length

    def encode_node(bin_path, index='0'):
        prim = bin_types[bin_path]

        if prim == 'pair':
            return dict(
                prim='Pair',
                args=list(map(lambda x: encode_node(bin_path + x, index), '01'))
            )
        elif prim in ['map', 'big_map']:
            length = get_length(bin_path, index)
            return [
                dict(
                    prim='Elt',
                    args=[encode_node(bin_path + '0', f'{index}:{i}'),
                          encode_node(bin_path + '1', f'{index}:{i}')]
                )
                for i in range(length)
            ]
        elif prim in ['set', 'list']:
            length = get_length(bin_path, index)
            return [
                encode_node(bin_path + '0', f'{index}:{i}')
                for i in range(length)
            ]
        elif prim == 'or':
            entry = bin_values[bin_path][index]
            return dict(
                prim={'0': 'Left', '1': 'Right'}[entry],
                args=[encode_node(bin_path + entry, index)]
            )
        elif prim == 'option':
            try:
                option = encode_node(bin_path + '0', index)
                if option:
                    return dict(prim='Some', args=[option])
                else:
                    return dict(prim='None')
            except KeyError:
                return dict(prim='None')
        else:
            value = bin_values[bin_path][index]
            if value is None:
                return None
            else:
                return encode_literal(value, prim)

    return encode_node(root)


def build_schema(code) -> Schema:
    metadata = collapse_micheline(code)
    return Schema(metadata, *build_maps(metadata))


def decode_micheline(data, schema: Schema, root='0'):
    json_root = schema.bin_to_json[root]
    json_values = parse_micheline(data, schema.bin_to_json, schema.bin_types, root)
    return make_json(json_values, schema.json_types, json_root)


def encode_micheline(data, schema: Schema, root='0'):
    json_root = schema.bin_to_json[root]
    bin_values = parse_json(data, schema.json_to_bin, schema.bin_types, json_root)
    return make_micheline(bin_values, schema.bin_types, root)
