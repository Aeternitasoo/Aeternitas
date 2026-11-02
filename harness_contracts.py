"""Small explicit JSON Schema subset for registered tools and final outputs.

Unsupported keywords are refused at registration, never silently ignored.
The model decision envelope is checked separately by the Harness.
"""
import json
import math
import re


class HarnessError(ValueError):
    def __init__(self, code):
        self.code = code if isinstance(code, str) and re.fullmatch(r'[a-z][a-z0-9_:]{0,120}', code) else 'invalid_error_code'
        super().__init__(self.code)


KEYS = {'type', 'properties', 'required', 'additionalProperties', 'items', 'enum',
        'minimum', 'maximum', 'minLength', 'maxLength', 'pattern', 'minItems', 'maxItems',
        'description', 'title'}


def checked_schema(schema, depth=0):
    if not isinstance(schema, dict) or depth > 8 or set(schema) - KEYS:
        raise HarnessError('unsupported_schema')
    kind = schema.get('type')
    if kind not in {'object', 'array', 'string', 'integer', 'number', 'boolean', 'null'}:
        raise HarnessError('unsupported_schema_type')
    applicable = {'type', 'enum', 'description', 'title'} | {
        'object': {'properties', 'required', 'additionalProperties'},
        'array': {'items', 'minItems', 'maxItems'}, 'string': {'minLength', 'maxLength', 'pattern'},
        'integer': {'minimum', 'maximum'}, 'number': {'minimum', 'maximum'},
        'boolean': set(), 'null': set()}[kind]
    if set(schema) - applicable:
        raise HarnessError('inapplicable_schema_keyword')
    if 'enum' in schema and (not isinstance(schema['enum'], list) or not schema['enum']):
        raise HarnessError('invalid_schema_enum')
    if kind == 'object':
        props, required = schema.get('properties'), schema.get('required')
        if (not isinstance(props, dict) or not isinstance(required, list)
                or any(not isinstance(k, str) for k in props)
                or any(not isinstance(k, str) for k in required)
                or len(set(required)) != len(required) or set(required) - set(props)
                or schema.get('additionalProperties') is not False):
            raise HarnessError('closed_object_schema_required')
        for value in props.values():
            checked_schema(value, depth + 1)
    if kind == 'array':
        checked_schema(schema.get('items'), depth + 1)
    for low, high in [('minLength', 'maxLength'), ('minItems', 'maxItems')]:
        for key in (low, high):
            if key in schema and (type(schema[key]) is not int or not 0 <= schema[key] <= 32768):
                raise HarnessError('invalid_schema_bound')
        if schema.get(low, 0) > schema.get(high, 32768):
            raise HarnessError('invalid_schema_bound')
    for key in ('minimum', 'maximum'):
        if key in schema and (type(schema[key]) not in (int, float) or not math.isfinite(schema[key])):
            raise HarnessError('invalid_schema_bound')
    if schema.get('minimum', float('-inf')) > schema.get('maximum', float('inf')):
        raise HarnessError('invalid_schema_bound')
    if 'pattern' in schema:
        if not isinstance(schema['pattern'], str) or len(schema['pattern']) > 128:
            raise HarnessError('invalid_schema_pattern')
        re.compile(schema['pattern'])
    return schema


def finite_json(value, limit=32768):
    try:
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
    except (ValueError, TypeError):
        raise HarnessError('invalid_json_value') from None
    if len(raw) > limit:
        raise HarnessError('json_byte_limit')
    return raw


def validate(value, schema, depth=0):
    if depth == 0:
        finite_json(value)
    kind = schema['type']
    good = {'object': isinstance(value, dict), 'array': isinstance(value, list),
            'string': isinstance(value, str), 'integer': type(value) is int,
            'number': type(value) in (int, float), 'boolean': type(value) is bool,
            'null': value is None}[kind]
    if not good or depth > 8:
        raise HarnessError('argument_type')
    if 'enum' in schema and value not in schema['enum']:
        raise HarnessError('argument_enum')
    if kind == 'object':
        if set(value) - set(schema['properties']) or set(schema['required']) - set(value):
            raise HarnessError('argument_fields')
        for key, child in value.items():
            validate(child, schema['properties'][key], depth + 1)
    if kind == 'array':
        if not schema.get('minItems', 0) <= len(value) <= schema.get('maxItems', 1024):
            raise HarnessError('argument_items')
        for child in value:
            validate(child, schema['items'], depth + 1)
    if kind == 'string':
        if (not schema.get('minLength', 0) <= len(value) <= schema.get('maxLength', 4096)
                or ('pattern' in schema and re.search(schema['pattern'], value) is None)):
            raise HarnessError('argument_string')
    if kind in {'integer', 'number'} and not schema.get('minimum', float('-inf')) <= value <= schema.get('maximum', float('inf')):
        raise HarnessError('argument_range')
    return value
