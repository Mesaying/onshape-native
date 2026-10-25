"""Build editable templates from the current server's feature specifications.

Specs are cached by what actually changes them, not by the Part Studio microversion:
standard features (namespace '') by FeatureScript libraryVersion, custom features by namespace
(e<eid>::m<id> names one Feature Studio revision, so its spec never changes). A same-document custom spec
is looked up in that Feature Studio's workspace featurespecs (separate rate-limit family, the same call
featurestudio_patch uses); if the namespace is not there, the Part Studio specs are read instead."""
import hashlib
import json
import re

from .client import OnshapeError

NAMESPACE = re.compile(r"^e([a-f0-9]{24})::m([a-f0-9]{24})$")


def template(spec):
    def clean(value):
        if isinstance(value, list): return [clean(x) for x in value]
        if isinstance(value, dict): return {k:clean(v) for k,v in value.items() if k != 'nodeId'}
        return value
    return {'btType':'BTMFeature-134', 'featureType':spec['featureType'],
            'name':spec.get('featureTypeName') or spec['featureType'],
            'namespace':spec.get('namespace',''), 'suppressed':False,
            'parameters':[clean(p['defaultValue']) for p in spec.get('parameters',[]) if p.get('defaultValue') is not None]}


def spec_dir(service):
    return service.store.root / 'specs' if hasattr(service.store, 'root') else None


def spec_file(service, kind, *parts):
    root = spec_dir(service)
    if root is None:
        return None
    return root / (kind + '-' + hashlib.sha256(json.dumps(parts).encode()).hexdigest()[:32] + '.json')


def load(file):
    try:
        return json.loads(file.read_text()) if file and file.is_file() else None
    except (OSError, ValueError):
        return None


def save(file, value):
    if not file:
        return
    try:
        file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        file.write_text(json.dumps(value))
    except OSError:
        pass


def spec_key(service, spec, library):
    namespace = spec.get('namespace', '')
    return spec_file(service, 'spec', 'ns' if namespace else 'lib:' + str(library), namespace, spec['featureType'])


def remember_specs(service, specs, library):
    for spec in specs:
        if isinstance(spec, dict) and spec.get('featureType'):
            save(spec_key(service, spec, library), spec)


def forget_lists(service):
    """Feature Studio changed: cached Part Studio feature lists may miss new namespaces."""
    root = spec_dir(service)
    if root is None or not root.is_dir():
        return
    for file in root.glob('list-*.json'):
        try:
            file.unlink()
        except OSError:
            pass


def listing(specs):
    return [{'feature_type':s['featureType'],'name':s.get('featureTypeName'),'namespace':s.get('namespace','')} for s in specs]


async def part_studio_specs(service, data):
    raw = await service.call('getPartStudioFeatureSpecs', service.pinned(data))
    library = raw.get('libraryVersion', data.get('libraryVersion'))
    remember_specs(service, raw.get('featureSpecs', []), library)
    return raw


async def discover(service, snapshot, feature_type='', namespace='', refresh=False):
    data = service.snapshot_data(snapshot)
    library = data.get('libraryVersion')
    target = data['target']
    list_file = spec_file(service, 'list', target['did'], target['eid'], library, data['configuration'])
    if not feature_type:
        cached = None if refresh else load(list_file)
        if cached:
            return {'specifications': cached['artifact'], 'features': cached['features'], 'cached': True, 'api_calls': 0,
                    'next': 'Cached for this Part Studio and FeatureScript version. Pass refresh=true after importing new custom features.'}
        raw = await part_studio_specs(service, data)
        artifact = service.store.put(raw)['artifact']
        features = listing(raw.get('featureSpecs', []))
        save(list_file, {'artifact': artifact, 'features': features})
        return {'specifications': artifact, 'features': features,
                'next': 'Request an exact feature_type and namespace for defaults and parameter constraints.'}
    probe = {'featureType': feature_type, 'namespace': namespace}
    spec = None if refresh else load(spec_key(service, probe, library))
    calls, source = 0, 'cache'
    if spec is None:
        match = NAMESPACE.match(namespace or '')
        specs, matches = [], []
        if match and target['wvm'] == 'w':
            # Feature Studio budget, not the Part Studio one. Only an exact namespace match is used.
            try:
                raw = await service.client.request('GET', f"/api/v10/featurestudios/d/{target['did']}/w/{target['wvmid']}/e/{match.group(1)}/featurespecs")
                specs = [s for s in raw.get('featureSpecs', []) if isinstance(s, dict) and s.get('namespace')]
                calls += 1
            except OnshapeError:
                specs = []
            matches = [s for s in specs if s['featureType'] == feature_type and s.get('namespace') == namespace]
            source = 'feature studio'
        if not matches:
            raw = await part_studio_specs(service, data)
            specs = raw.get('featureSpecs', [])
            calls += 1
            source = 'part studio'
            matches = [s for s in specs if s['featureType'] == feature_type and s.get('namespace', '') == namespace]
        remember_specs(service, specs, library)
        if len(matches) != 1:
            raise OnshapeError('Feature type/namespace must match exactly one live specification. List them first.')
        spec = matches[0]
    return {'microversion': data['microversion'], 'libraryVersion': library, 'template': template(spec), 'specification': spec,
            'source': source, 'api_calls': calls,
            'next': 'Resolve required geometry queries and set parameters, then feature(action=add). Defaults alone may not form valid geometry.'}
