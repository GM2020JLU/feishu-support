"""Explicit scope/context fact content inventory, never clearing authority."""
import json
import re
import copy

from .ids import digest

POLICY = 'fact-content-fields-v1'
FIELDS = {'product','component','board','software_version','boot_stage','storage_medium'}
TOP = {'schema_version','policy','query_digest','supplied_digest','mentions','fields','observed_scope'}
MENTION = {'field','value','text','status','role','reason','source'}
CONTEXT = {'version_component','subject','event_order','author_id','author_role','explicit_correction'}
SOURCE = {'kind','input_digest','field','verification','start','end','event_pk',
          'message_id','event_digest','subject'}


def fact_json_fields(raw):
    def unique(pairs):
        result = {}
        for key,item in pairs:
            if key in result:
                raise ValueError('duplicate key')
            result[key] = item
        return result
    try:
        if not isinstance(raw,str) or len(raw.encode()) > 262144:
            raise ValueError('input limit')
        return fact_fields(json.loads(raw,object_pairs_hook=unique))
    except (ValueError,TypeError,UnicodeError,RecursionError):
        return fact_fields(None)


def fact_fields(value):
    """Count original/derived strings without exposing text or source identities.

    Retained identities have independent privacy obligations. Recognizing the
    schema does not prove reference coverage, authenticity or permission to clear.
    """
    base = {'policy':POLICY,'shape_recognized':False,'clear_allowed':False,
            'body_bytes':0,'body_paths':[]}
    try:
        encoded = json.dumps(value,ensure_ascii=False,allow_nan=False)
        if len(encoded.encode()) > 262144:
            raise ValueError('size')
        if (not isinstance(value,dict) or set(value)!=TOP
                or type(value['schema_version']) is not int or value['schema_version']!=1
                or value['policy'] not in {'observed-polarity-provenance-v2','conversation-facts-v3'}):
            raise ValueError('schema')
        context = value['policy']=='conversation-facts-v3'
        if any(not isinstance(value[key],str) or not re.fullmatch('[a-f0-9]{64}',value[key])
               for key in ('query_digest','supplied_digest')):
            raise ValueError('digest metadata')
        body = []
        def text(path, item, nullable=False):
            if item is None and nullable:
                return
            if not isinstance(item,str):
                raise ValueError('text type')
            body.append((path,len(item.encode())))
        if not isinstance(value['mentions'],list) or len(value['mentions'])>4096:
            raise ValueError('mentions')
        for index,item in enumerate(value['mentions']):
            expected = MENTION | (CONTEXT if context else set())
            if (not isinstance(item,dict) or set(item)!=expected or item['field'] not in FIELDS
                    or not isinstance(item['status'],str) or not isinstance(item['role'],str)
                    or not isinstance(item['source'],dict) or set(item['source'])-SOURCE
                    or item['source'].get('kind') not in {'query','supplied','context_event'}
                    or any(isinstance(v,(dict,list)) for v in item['source'].values())):
                raise ValueError('mention schema')
            source = item['source']
            source_keys = ({'kind','input_digest','start','end','verification','event_pk',
                            'message_id','event_digest','subject'} if context else
                           {'kind','input_digest','field','verification'} if source['kind']=='supplied'
                           else {'kind','input_digest','start','end','verification'})
            if (set(source)!=source_keys or (context and source['kind']!='context_event')
                    or (not context and source['kind'] not in {'query','supplied'})
                    or any(not isinstance(v,str) for k,v in source.items() if k not in {'start','end'})
                    or any(type(source[k]) is not int or source[k]<0 for k in ('start','end') if k in source)):
                raise ValueError('source metadata')
            if context and (not isinstance(item['subject'],str)
                    or type(item['event_order']) not in {int,float}
                    or type(item['explicit_correction']) is not bool
                    or not isinstance(item['author_role'],str)
                    or (item['author_id'] is not None and not isinstance(item['author_id'],str))):
                raise ValueError('context metadata')
            for key in ('text','value','reason'):
                text(f'/mentions/{index}/{key}',item[key])
            if context:
                text(f'/mentions/{index}/version_component',item['version_component'],True)
        if not isinstance(value['fields'],dict) or set(value['fields'])-FIELDS:
            raise ValueError('fields')
        for name,item in value['fields'].items():
            expected = {'state','value','excluded_values','mention_indexes'} | (
                {'current_mention_indexes'} if context else set())
            if not isinstance(item,dict) or set(item)!=expected or not isinstance(item['excluded_values'],list):
                raise ValueError('field schema')
            if item['state'] not in {'known','unknown','conflict'}:
                raise ValueError('field state')
            for key in ('mention_indexes','current_mention_indexes') if context else ('mention_indexes',):
                if (not isinstance(item[key],list)
                        or any(type(i) is not int or not 0 <= i < len(value['mentions']) for i in item[key])):
                    raise ValueError('mention reference')
            text(f'/fields/{name}/value',item['value'],True)
            for index,excluded in enumerate(item['excluded_values']):
                text(f'/fields/{name}/excluded_values/{index}',excluded)
        if not isinstance(value['observed_scope'],dict) or set(value['observed_scope'])-FIELDS:
            raise ValueError('scope')
        for name,item in value['observed_scope'].items():
            text(f'/observed_scope/{name}',item)
        return {**base,'shape_recognized':True,'body_bytes':sum(size for _,size in body),
                'body_paths':[path for path,_ in body],'input_digest':digest(value),
                'scope':'registered_fact_content_only_not_retirement'}
    except (ValueError,TypeError,KeyError,UnicodeError,RecursionError):
        return {**base,'reason':'unclassified_fact_shape'}


def redact_fact_content(value, *, expected_digest):
    """Pure tombstone candidate, not an active fact object or a database writer.

    Null placeholders preserve array indexes and source metadata. The enclosing
    retirement transaction must still gate all references and revoke consumers.
    """
    classified = fact_fields(value)
    if not classified['shape_recognized']:
        raise ValueError('unclassified facts cannot be transformed')
    if not isinstance(expected_digest,str) or expected_digest != classified['input_digest']:
        raise ValueError('facts changed since classification')
    retained = copy.deepcopy(value)
    for path in classified['body_paths']:
        parts = path.split('/')[1:]
        target = retained
        for part in parts[:-1]:
            target = target[int(part)] if isinstance(target,list) else target[part]
        key = int(parts[-1]) if isinstance(target,list) else parts[-1]
        target[key] = None
    tombstone = {'schema':'retired-fact-content-v1','content_state':'retired',
                 'original_digest':expected_digest,'classification_policy':POLICY,
                 'retained_metadata':retained}
    return {'value':tombstone,'input_digest':expected_digest,'output_digest':digest(tombstone),
            'removed_body_bytes':classified['body_bytes'],'clear_allowed':False,
            'scope':'pure_candidate_transform_only'}
