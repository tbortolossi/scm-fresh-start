"""Offline tests: the planning and neutralising logic, against a fake tenant."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import scm_preclean as P


class FakeScm:
    """Answers list calls from a dict {endpoint path: [items]}; records writes."""

    def __init__(self, data, refuse=()):
        self.data, self.refuse, self.calls = data, set(refuse), []

    def list(self, path, **params):
        return [x for x in self.data.get(path, [])
                if params.get('folder') in (None, x.get('folder'), 'ngfw-shared')]

    def call(self, method, path, **kw):
        self.calls.append((method, path, kw.get('json')))
        if method == 'DELETE' and path.split('/')[-1] in self.refuse:
            raise RuntimeError('409 Another entity is currently referencing this object')
        if method == 'GET':
            return self.data.get(path, {})
        return {}


TENANT = {
    '/config/setup/v1/snippets': [{'name': 'Auto-VPN-Default-Snippet', 'type': 'readonly'},
                                  {'name': 'my-snippet', 'type': None}],
    '/config/setup/v1/folders': [{'id': 'f1', 'name': 'ngfw-shared', 'parent': 'All',
                                  'snippets': ['Auto-VPN-Default-Snippet', 'my-snippet']}],
    '/config/network/v1/zones': [
        {'id': 'z1', 'name': 'internet', 'folder': 'ngfw-shared',
         'network': {'layer3': ['$eth-internet'], 'zone_protection_profile': 'best-practice'}},
        {'id': 'z2', 'name': 'proxy', 'folder': 'ngfw-shared'},
        {'id': 'z3', 'name': 'zone-internal', 'folder': 'ngfw-shared', 'snippet': 'Auto-VPN-Default-Snippet'},
    ],
    '/config/network/v1/ethernet-interfaces': [
        {'id': 'e1', 'name': '$eth-internet', 'folder': 'ngfw-shared', 'layer3': {}, 'slot': 1}],
    '/config/network/v1/zones/z1': {
        'id': 'z1', 'name': 'internet', 'folder': 'ngfw-shared',
        'network': {'layer3': ['$eth-internet'], 'zone_protection_profile': 'best-practice'}},
    '/config/network/v1/ethernet-interfaces/e1': {
        'id': 'e1', 'name': '$eth-internet', 'folder': 'ngfw-shared', 'layer3': {}, 'slot': 1,
        'default_value': 'ethernet1/3'},
    '/config/security/v1/security-rules': [
        {'id': 'm1', 'name': 'Auto-VPN-Default-Snippet', 'folder': 'ngfw-shared', 'policy_type': 'Security'}],
}


def test_rule_marker_skipped_but_bare_zone_kept():
    assert not P.is_own('security/v1/security-rules', 'ngfw-shared',
                        {'id': 'm', 'name': 'x', 'folder': 'ngfw-shared', 'policy_type': 'Security'})
    assert P.is_own('network/v1/zones', 'ngfw-shared', {'id': 'z', 'name': 'proxy', 'folder': 'ngfw-shared'})


def test_plan_owns_only_folder_items_and_detaches_only_predefined():
    todo, kept = P.plan(FakeScm(TENANT), ['ngfw-shared'])
    got = [P.describe(t) for t in todo]
    assert '[All Firewalls] delete zones `internet`' in got
    assert '[All Firewalls] delete zones `proxy`' in got
    assert not any('zone-internal' in d for d in got)            # lives in a snippet
    assert not any('security-rules' in d for d in got)           # snippet marker
    assert '[All Firewalls] detach snippet `Auto-VPN-Default-Snippet`' in got
    assert not any('my-snippet' in d for d in got)               # user snippet stays
    assert kept == []


def test_referenced_zone_and_interface_are_neutralised():
    fake = FakeScm(TENANT, refuse={'z1', 'e1'})
    todo = [t for t in P.plan(fake, ['ngfw-shared'])[0] if t[3].get('id') in ('z1', 'e1')]
    done, neutral, still = P.apply(fake, todo)
    assert still == [] and done == []
    assert neutral == {('ngfw-shared', 'zones', 'internet'), ('ngfw-shared', 'ethernet-interfaces', '$eth-internet')}
    puts = {p: body for m, p, body in fake.calls if m == 'PUT'}
    assert puts['/config/network/v1/zones/z1']['network']['layer3'] == []
    assert 'default_value' not in puts['/config/network/v1/ethernet-interfaces/e1']


def test_emptied_zone_is_none_when_nothing_to_remove():
    assert P.emptied_zone({'name': 'z', 'network': {'layer3': []}}) is None
    assert P.without_port({'name': '$x'}) is None


def test_credentials_prefix(monkeypatch):
    env = {'SCM_TEST_TSG_ID': '1', 'SCM_TEST_CLIENT_ID': 'c', 'SCM_TEST_CLIENT_SECRET': 's'}
    assert P.credentials(env, 'SCM_TEST') == ['1', 'c', 's']


def test_unref_rule_keeps_the_rule_valid():
    names = {'All Web Applications', 'Web Security Global', 'High Risk Applications', 'New Web Applications'}
    vm = {'id': 'r', 'name': 'default-trust-to-trust-zone-policy', 'snippet': 'Azure-VM-Default',
          'application': ['All Web Applications'], 'action': 'allow'}
    assert P.unref_rule(vm, names)['application'] == ['any']
    web = {'id': 'w', 'name': 'Global Web Access-Allow', 'policy_type': 'Internet', 'tag': ['Web Security Global'],
           'allow_web_application': [{'name': 'New Web Applications'}], 'allow_url_category': [{'name': 'news'}]}
    body = P.unref_rule(web, names)
    assert 'tag' not in body and 'allow_web_application' not in body and body['allow_url_category']
    assert P.unref_rule({'id': 'x', 'name': 'x', 'application': ['ssl']}, names) is None


def test_unref_group_zone_and_filter():
    pg = {'id': 'g', 'name': 'DNS-Best-Practice-pg', 'spyware': ['web-security-default'], 'url_filtering': ['x']}
    assert P.unref_group(pg, {'web-security-default'}) == {
        'name': 'DNS-Best-Practice-pg', 'spyware': ['best-practice'], 'url_filtering': ['x']}
    z = {'id': 'z', 'name': 'trust-zone', 'network': {'layer3': ['$t'], 'zone_protection_profile': 'best-practice'}}
    assert P.unref_zone(z, {'best-practice'})['network'] == {'layer3': ['$t']}
    af = {'id': 'a', 'name': 'Tolerated Gen AI Apps', 'tagging': {'tag': ['[Web App]', 'Tolerated']}}
    assert P.untag_filter(af, {'Tolerated'})['tagging'] == {'tag': ['[Web App]']}
    assert 'tagging' not in P.untag_filter({'id': 'b', 'name': 'b', 'tagging': {'tag': ['Tolerated']}}, {'Tolerated'})


def test_swg_zone_step_planned_first_when_not_any():
    fake = FakeScm(TENANT)
    fake.ui_api = 'https://ui.example'
    fake.ui = lambda method, path, **kw: {'inbound_zone': 'any', 'outbound_zone': 'internet'}
    todo, _ = P.plan(fake, ['ngfw-shared'])
    assert todo[0][0] == 'swg'
    assert 'outbound internet -> any' in P.describe(todo[0])
