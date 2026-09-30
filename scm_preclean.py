#!/usr/bin/env python3
"""Strip a Strata Cloud Manager tenant of its default NGFW configuration before a Panorama import.

    python scm_preclean.py                     # plan only: list what would change
    python scm_preclean.py --apply             # clean "All Firewalls"
    python scm_preclean.py --apply --global    # also clean "Global"
    python scm_preclean.py --apply --global --deep   # also edit predefined snippets that pin Global

Credentials come from the environment or a .env file: SCM_TSG_ID, SCM_CLIENT_ID,
SCM_CLIENT_SECRET (a service account of the tenant). --env-prefix SCM_TEST reads
SCM_TEST_TSG_ID and so on, to point the same .env at a second tenant.

Why: a fresh tenant is not empty the way a freshly booted Panorama is. "All
Firewalls" (`ngfw-shared`) owns the interfaces `$eth-internet` and `$eth-local`,
whose default ports are ethernet1/3 and ethernet1/4. A Panorama import that
migrates a template using those ports gets them refused ("… 'ethernet1/3' is
already in use … Discarding"), and the interfaces silently disappear from the
imported firewalls. A push of that import would delete them on the device.

What it does, in the chosen folders only, each step before what it references:
1. delete every item the folder owns itself (not through a snippet): rules,
   logical and virtual routers, zones, interfaces, profiles, objects, variables;
2. a zone that stays referenced is kept, with its interfaces removed;
3. an interface that still cannot be deleted has its default port cleared;
4. detach every predefined/readonly snippet, one at a time.

Without --deep, what SCM refuses to remove is reported as "kept", with the reason
(see KEPT). The blockers are predefined snippets attached nowhere (VM templates,
best-practice bundles) that reference objects of Global's snippets and tags.
--deep edits those referrers first, minimally (see plan_unref), so that Global
can be emptied too.
Every item touched is saved to a JSON backup before the first change. Changes
stay in the candidate configuration: nothing is pushed.
"""
import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

import requests

__version__ = '0.2.0'

API = 'https://api.strata.paloaltonetworks.com'
AUTH = 'https://auth.apps.paloaltonetworks.com/oauth2/access_token'
FOLDERS = {'ngfw-shared': 'All Firewalls', 'All': 'Global'}

# Deletion order: a referrer always before what it refers to.
RULES = ['security/v1/security-rules', 'network/v1/nat-rules', 'security/v1/decryption-rules',
         'security/v1/app-override-rules', 'network/v1/pbf-rules', 'network/v1/qos-policy-rules',
         'security/v1/dos-protection-rules', 'identity/v1/authentication-rules']
ITEMS = [
    'network/v1/logical-routers', 'network/v1/virtual-routers',
    'network/v1/ipsec-tunnels', 'network/v1/ike-gateways',
    'network/v1/zones',
    'network/v1/layer3-subinterfaces', 'network/v1/vlan-interfaces', 'network/v1/tunnel-interfaces',
    'network/v1/loopback-interfaces',
    'network/v1/ethernet-interfaces',
    'network/v1/ike-crypto-profiles', 'network/v1/ipsec-crypto-profiles',
    'network/v1/interface-management-profiles', 'network/v1/zone-protection-profiles',
    'network/v1/dns-proxies',
    'security/v1/profile-groups',
    'security/v1/anti-spyware-profiles', 'security/v1/vulnerability-protection-profiles',
    'security/v1/wildfire-anti-virus-profiles', 'security/v1/url-access-profiles',
    'security/v1/file-blocking-profiles', 'security/v1/dns-security-profiles',
    'security/v1/decryption-profiles', 'security/v1/url-categories',
    'objects/v1/log-forwarding-profiles', 'objects/v1/syslog-server-profiles',
    'objects/v1/address-groups', 'objects/v1/addresses', 'objects/v1/service-groups',
    'objects/v1/services', 'objects/v1/application-groups', 'objects/v1/application-filters',
    'objects/v1/external-dynamic-lists', 'objects/v1/dynamic-user-groups', 'objects/v1/schedules',
    'objects/v1/tags',
    'setup/v1/variables',
]
# In a rulebase, a folder lists each attached snippet as an entry with only these
# keys: a position marker, not a rule. A bare zone has the same keys and is real.
MARKER = {'id', 'name', 'folder', 'snippet', 'policy_type'}

# What SCM does not let go, and why: not retried, not a failure. The tenant's own
# certificates (Root CA, Forward-Trust-CA, cookie CAs...) are not even attempted:
# "Deleting default certificates is not allowed".
KEPT = {
    ('All', 'tags', 'Sanctioned'): 'referenced by the predefined snippet Gen-AI-Best-Practice',
    ('All', 'tags', 'Tolerated'): 'referenced by the predefined snippet Gen-AI-Best-Practice',
    # UI names Global-Default / Internet-Security-Default. Their referrers are
    # predefined snippets attached nowhere, which cannot be deleted either.
    ('All', 'snippet', 'default'): 'the predefined VM snippets (AWS/Azure/GCP) reference its '
                                   'best-practice zone protection',
    ('All', 'snippet', 'Web-Security-Default'): 'the predefined VM, DNS-Best-Practice and '
                                                'Internet-Access-Best-Practice snippets reference it',
    # Holds the `Local Users` authentication profile for GlobalProtect / Mobile Users;
    # detaching it fails with a bare 500 even under --deep. Not NGFW config.
    ('All', 'snippet', 'GlobalProtect-Default'): 'GlobalProtect / Mobile Users use its authentication '
                                                 'profile (SCM answers a bare 500); not NGFW config',
}


# The one blocker no API role reaches: set it by hand, then rerun.
SWG_HINT = ('referenced by swg/general-settings/outbound-zone: in the UI, Internet Security > General > '
            'General Settings (scope All Firewalls), set Outbound Zone to `any`, save, rerun')


# ------------------------------------------------------------------ settings
def load_env(path='.env'):
    """os.environ, overlaid on KEY=value lines of a .env file if there is one."""
    env = {}
    p = Path(path)
    if p.is_file():
        for line in p.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    env.update(os.environ)
    return env


def credentials(env, prefix='SCM'):
    keys = [f'{prefix}_{k}' for k in ('TSG_ID', 'CLIENT_ID', 'CLIENT_SECRET')]
    missing = [k for k in keys if not env.get(k)]
    if missing:
        raise SystemExit(f'missing: {", ".join(missing)} (environment or .env)')
    return [env[k] for k in keys]


# ------------------------------------------------------------------ SCM API
class Scm:
    def __init__(self, tsg, client_id, secret):
        r = requests.post(AUTH, data={'grant_type': 'client_credentials', 'scope': f'tsg_id:{tsg}'},
                          auth=(client_id, secret), timeout=60)
        r.raise_for_status()
        self.h = {'Authorization': f'Bearer {r.json()["access_token"]}'}

    def list(self, path, **params):
        """Every page of a list endpoint; [] when the endpoint is not available."""
        out, offset, prev = [], 0, None
        while True:
            r = requests.get(API + path, headers=self.h, timeout=120,
                             params={**params, 'limit': 200, 'offset': offset})
            if r.status_code in (400, 403, 404, 500):
                if offset == 0:
                    print(f'  ! {path.split("/")[-1]}: HTTP {r.status_code}, skipped')
                return out
            r.raise_for_status()
            j = r.json()
            data = j.get('data', []) if isinstance(j, dict) else j
            ids = [d.get('id') for d in data]
            if ids and ids == prev:          # some endpoints ignore offset
                return out
            prev, out = ids, out + data
            total = j.get('total') if isinstance(j, dict) else None
            if len(data) < 200 or (total is not None and len(out) >= total):
                return out
            offset += 200

    def call(self, method, path, **kw):
        r = requests.request(method, API + path, headers=self.h, timeout=120, **kw)
        if r.status_code >= 400:
            try:
                e = r.json()['_errors'][0]
                # The first message is often generic ("Your configuration is not
                # valid"); the specifics sit in details.
                det = e.get('details') or {}
                extra = [x.get('message') or x.get('msg') for x in det.get('errors') or []]
                extra += det.get('message') if isinstance(det.get('message'), list) else [det.get('message')]
                msg = '. '.join([e['message'], *[x for x in extra if x]])
            except (ValueError, KeyError, IndexError, TypeError, AttributeError):
                msg = r.text[:300]
            raise RuntimeError(f'{r.status_code} {msg}')
        return r.json() if r.text.strip() else {}


# ------------------------------------------------------------------ logic
def kind(ep):
    return ep.split('/')[-1]


def is_own(ep, folder, item):
    """Owned by the folder itself, not by a snippet, and not a rulebase marker."""
    return (item.get('folder') == folder and not item.get('snippet')
            and not ('rules' in ep and set(item) <= MARKER))


def plan(c, folders, deep=False):
    """(action, folder, endpoint, item) still to do, and (description, reason) kept."""
    kept_rules = {k: v for k, v in KEPT.items() if k[2] == 'GlobalProtect-Default'} if deep else KEPT
    snippets = {s['name']: s.get('type') for s in c.list('/config/setup/v1/snippets')}
    fobj = {f['name']: f for f in c.list('/config/setup/v1/folders')}
    todo, kept = [], []
    for folder in folders:
        found = []
        for ep in RULES:
            for pos in ('pre', 'post'):
                found += [(ep, x) for x in c.list('/config/' + ep, folder=folder, position=pos)]
        found += [(ep, x) for ep in ITEMS for x in c.list('/config/' + ep, folder=folder)]
        for ep, x in found:
            if not is_own(ep, folder, x):
                continue
            why = kept_rules.get((folder, kind(ep), x.get('name')))
            if why:
                kept.append((f'[{FOLDERS[folder]}] {kind(ep)} `{x["name"]}`', why))
            else:
                todo.append(('delete', folder, ep, x))
        for s in fobj.get(folder, {}).get('snippets') or []:
            if snippets.get(s) not in ('predefined', 'readonly'):
                continue
            why = kept_rules.get((folder, 'snippet', s))
            if why:
                kept.append((f'[{FOLDERS[folder]}] snippet `{s}` attached', why))
            else:
                todo.append(('detach', folder, None, {'name': s, 'folder': fobj[folder]}))
    if deep:
        detached = [t[3]['name'] for t in todo if t[0] == 'detach']
        tags = {t[3]['name'] for t in todo if t[0] == 'delete' and kind(t[2]) == 'tags'}
        todo = plan_unref(c, snippets, folders, detached, tags) + todo
    return todo, kept


# ------------------------------------------------------------------ --deep
# Object kinds of a detached snippet that other snippets were seen to reference.
REFERENCED = ['objects/v1/application-filters', 'objects/v1/tags',
              'security/v1/anti-spyware-profiles', 'security/v1/vulnerability-protection-profiles',
              'security/v1/wildfire-anti-virus-profiles', 'security/v1/dns-security-profiles']
PG_KEYS = ('spyware', 'vulnerability', 'dns_security', 'virus_and_wildfire_analysis')


def unref_rule(rule, names):
    """A security rule's body without references to `names`, or None if it has none.

    An emptied application list becomes `any`: the rule keeps matching something
    rather than turning invalid."""
    body = {k: v for k, v in rule.items() if k not in ('id', 'folder', 'snippet')}
    changed = False
    if set(body.get('application') or []) & names:
        body['application'] = [a for a in body['application'] if a not in names] or ['any']
        changed = True
    if set(body.get('tag') or []) & names:
        body['tag'] = [t for t in body['tag'] if t not in names]
        changed = True
        if not body['tag']:
            del body['tag']
    for key in ('block_web_application', 'allow_web_application'):
        vals = body.get(key) or []
        keep = [v for v in vals if (v.get('name') if isinstance(v, dict) else v) not in names]
        if len(keep) != len(vals):
            changed = True
            if keep:
                body[key] = keep
            else:
                del body[key]
    return body if changed else None


def unref_group(pg, names):
    """A profile group's body with profiles from `names` swapped for the predefined best-practice."""
    body = {k: v for k, v in pg.items() if k not in ('id', 'folder', 'snippet')}
    changed = False
    for key in PG_KEYS:
        if set(body.get(key) or []) & names and body[key] != ['best-practice']:
            body[key] = ['best-practice']
            changed = True
    return body if changed else None


def unref_zone(zone, profiles):
    if (zone.get('network') or {}).get('zone_protection_profile') not in profiles:
        return None
    body = {k: v for k, v in zone.items() if k not in ('id', 'folder', 'snippet')}
    body['network'] = {k: v for k, v in zone['network'].items() if k != 'zone_protection_profile'}
    return body


def untag_filter(af, tags):
    tg = (af.get('tagging') or {}).get('tag') or []
    if not set(tg) & tags:
        return None
    body = {k: v for k, v in af.items() if k not in ('id', 'folder', 'snippet', 'tagging')}
    rest = [t for t in tg if t not in tags]
    if rest:
        body['tagging'] = {'tag': rest}
    return body


def plan_unref(c, snippet_types, folders, detached, tags):
    """Minimal edits that release what the Global clean needs released.

    Tested on a fresh tenant: the predefined VM templates' zones use Global-Default's
    `best-practice` zone protection (so does All Firewalls' `internet` zone); their
    rules use Internet-Security-Default's application filters; DNS-Best-Practice-pg
    uses its profiles; Internet-Access-Best-Practice's rules its filters and tag;
    Gen-AI-Best-Practice's filters the Sanctioned/Tolerated tags."""
    names, zpp = set(), set()
    for sn in detached:
        for ep in REFERENCED:
            names |= {x['name'] for x in c.list('/config/' + ep, snippet=sn) if x.get('snippet') == sn}
        zpp |= {x['name'] for x in c.list('/config/network/v1/zone-protection-profiles', snippet=sn)
                if x.get('snippet') == sn}
    names |= tags
    scopes = [('snippet', n) for n, t in snippet_types.items()
              if (t in ('predefined', 'readonly') or n == 'predefined-snippet') and n not in detached]
    scopes += [('folder', f) for f in folders]
    out, seen = [], set()

    def add(scope, ep, item, body, note):
        if body is not None and item['id'] not in seen:
            seen.add(item['id'])
            out.append(('modify', scope[1], ep, {'item': item, 'body': body, 'note': note}))

    for scope in scopes:
        home = scope[1]

        def own(x, home=home):
            return (x.get('snippet') or x.get('folder')) == home
        q = {scope[0]: home}
        if zpp:
            for z in filter(own, c.list('/config/network/v1/zones', **q)):
                add(scope, 'network/v1/zones', z, unref_zone(z, zpp), 'drop zone protection ' + ', '.join(zpp))
        if names:
            for r in filter(own, c.list('/config/security/v1/security-rules', position='pre', **q)):
                add(scope, 'security/v1/security-rules', r, unref_rule(r, names), 'drop references')
            for pg in filter(own, c.list('/config/security/v1/profile-groups', **q)):
                add(scope, 'security/v1/profile-groups', pg, unref_group(pg, names), 'use best-practice')
        if tags:
            for af in filter(own, c.list('/config/objects/v1/application-filters', **q)):
                add(scope, 'objects/v1/application-filters', af, untag_filter(af, tags),
                    'drop tag ' + ', '.join(sorted(tags)))
    return out


def describe(t):
    action, folder, ep, x = t
    if action == 'modify':
        return f'[{folder}] edit {kind(ep)} `{x["item"].get("name")}`: {x["note"]}'
    return (f'[{FOLDERS[folder]}] detach snippet `{x["name"]}`' if action == 'detach'
            else f'[{FOLDERS[folder]}] delete {kind(ep)} `{x.get("name")}`')


def emptied_zone(zone):
    """The zone's body with every interface list emptied, or None if already empty."""
    net = dict(zone.get('network') or {})
    for mode in ('layer3', 'layer2', 'virtual_wire', 'tap', 'tunnel'):
        if net.get(mode):
            net[mode] = []
    if net == (zone.get('network') or {}):
        return None
    body = {k: v for k, v in zone.items() if k not in ('id', 'folder', 'snippet')}
    return {**body, 'network': net}


def without_port(iface):
    """The interface's body without its default port, or None if it has none."""
    if not iface.get('default_value'):
        return None
    return {k: v for k, v in iface.items() if k not in ('id', 'folder', 'snippet', 'default_value')}


def fallback(c, ep, x):
    """Deletion refused: neutralise instead. Returns what was done, or None."""
    if kind(ep) == 'zones':
        # Re-read: an earlier step of this run may have edited the zone.
        x = c.call('GET', f'/config/{ep}/{x["id"]}')
        body = emptied_zone(x)
        if body is None:
            return 'kept (still referenced), already without interfaces'
        c.call('PUT', f'/config/{ep}/{x["id"]}', json=body)
        return 'kept (still referenced), interfaces removed'
    if kind(ep) == 'ethernet-interfaces':
        # A list call does not return default_value; a GET by id does.
        full = c.call('GET', f'/config/{ep}/{x["id"]}')
        body = without_port(full)
        if body is None:
            return 'kept (still referenced), already without default port'
        c.call('PUT', f'/config/{ep}/{x["id"]}', json=body)
        return f'kept (still referenced), default port {full["default_value"]} released'
    return None


def modify(c, ep, x):
    item, body = x['item'], x['body']
    params = {'position': 'pre'} if 'rules' in ep else {}
    try:
        c.call('PUT', f'/config/{ep}/{item["id"]}', params=params, json=body)
    except RuntimeError as e:
        # An application filter an Internet rule allows cannot lose its tag
        # ("… is not a valid reference"): remove that rule, then retry.
        if kind(ep) != 'application-filters' or 'not a valid reference' not in str(e):
            raise
        sn = item.get('snippet')
        for r in c.list('/config/security/v1/security-rules', snippet=sn, position='pre'):
            if r.get('snippet') == sn and item['name'] in json.dumps(r):
                c.call('DELETE', f'/config/security/v1/security-rules/{r["id"]}')
                print(f'  ok    [{sn}] delete security-rules `{r["name"]}` (it pinned {item["name"]})')
        c.call('PUT', f'/config/{ep}/{item["id"]}', json=body)


def apply(c, todo):
    done, failed, neutral = [], [], set()
    for t in todo:
        action, folder, ep, x = t
        try:
            if action == 'modify':
                modify(c, ep, x)
            elif action == 'delete':
                c.call('DELETE', f'/config/{ep}/{x["id"]}')
            else:
                f = c.call('GET', f'/config/setup/v1/folders/{x["folder"]["id"]}')
                body = {k: f[k] for k in ('name', 'parent', 'description', 'labels') if f.get(k) is not None}
                body['snippets'] = [s for s in f.get('snippets') or [] if s != x['name']]
                c.call('PUT', f'/config/setup/v1/folders/{f["id"]}', json=body)
            done.append(describe(t))
            print(f'  ok    {describe(t)}')
        except RuntimeError as e:
            failed.append((t, str(e)))
    # Refused: retry once (the referrer may be gone by now), then neutralise.
    still = []
    for t, err in failed:
        action, folder, ep, x = t
        if action == 'modify':
            try:
                modify(c, ep, x)
                done.append(describe(t))
                print(f'  ok    {describe(t)} (second try)')
                continue
            except RuntimeError as e:
                err = str(e)
        elif action == 'delete':
            try:
                c.call('DELETE', f'/config/{ep}/{x["id"]}')
                done.append(describe(t))
                print(f'  ok    {describe(t)} (second try)')
                continue
            except RuntimeError as e:
                err = str(e)
        try:
            what = fallback(c, ep, x) if action == 'delete' else None
        except RuntimeError as e:
            what, err = None, f'{err}; fallback: {e}'
        if what:
            neutral.add((folder, kind(ep), x['name']))
            print(f'  kept  {describe(t)}: {what}')
            if 'outbound-zone' in err:
                print(f'        {SWG_HINT}')
        else:
            still.append(f'{describe(t)}: {err[:300]}')
            print(f'  FAIL  {describe(t)}: {err[:300]}')
    return done, neutral, still


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--apply', action='store_true', help='make the changes (default: plan only)')
    ap.add_argument('--global', dest='glob', action='store_true', help='also clean Global')
    ap.add_argument('--deep', action='store_true',
                    help='edit the predefined snippets that pin Global (implies --global)')
    ap.add_argument('--env-file', default='.env', help='KEY=value file read under the environment')
    ap.add_argument('--env-prefix', default='SCM',
                    help='read <PREFIX>_TSG_ID / _CLIENT_ID / _CLIENT_SECRET (default SCM)')
    ap.add_argument('--backup-dir', default='.', help='where the JSON backup is written')
    ap.add_argument('--version', action='version', version=__version__)
    a = ap.parse_args(argv)

    tsg, cid, secret = credentials(load_env(a.env_file), a.env_prefix)
    a.glob = a.glob or a.deep
    folders = ['ngfw-shared'] + (['All'] if a.glob else [])
    print(f'tenant TSG ...{tsg[-4:]}')
    c = Scm(tsg, cid, secret)
    todo, kept = plan(c, folders, a.deep)
    print(f'{len(todo)} change(s) in {", ".join(FOLDERS[f] for f in folders)}:')
    for t in todo:
        print(f'  - {describe(t)}')
    for d, why in kept:
        print(f'  = {d}: kept, {why}')
    if not a.apply or not todo:
        print('plan only: rerun with --apply' if todo else 'nothing left to remove')
        return 0

    backup = Path(a.backup_dir) / f'scm-preclean-{tsg[-4:]}-{dt.datetime.now(dt.timezone.utc):%Y%m%dT%H%M%SZ}.json'
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_text(json.dumps([{'action': k, 'scope': f, 'endpoint': ep, 'item': x}
                                  for k, f, ep, x in todo], indent=1, default=str))
    print(f'backup -> {backup}')
    done, neutral, still = apply(c, todo)
    left, _ = plan(c, folders, a.deep)
    left = [t for t in left if (t[1], kind(t[2] or ''), t[3].get('name')) not in neutral]
    print(f'{len(done)} removed, {len(neutral)} neutralised, {len(still)} failed; '
          + ('clean' if not left else f'{len(left)} left: ' + '; '.join(describe(t) for t in left)))
    return 1 if still or left else 0


if __name__ == '__main__':
    sys.exit(main())
