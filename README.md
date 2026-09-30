# scm-preclean

[![CI](https://github.com/tbortolossi/scm-preclean/actions/workflows/ci.yml/badge.svg)](https://github.com/tbortolossi/scm-preclean/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-green)](LICENSE)

**Strip a Strata Cloud Manager tenant of its default NGFW configuration before you import a Panorama
into it, so the import lands in a tenant as bare as a freshly booted Panorama.**

> Personal project, not a Palo Alto Networks product and not supported by Palo Alto Networks. It uses the
> public [SCM configuration API](https://pan.dev/scm/docs/home/). Try it on a test tenant first.

## Why

A new SCM tenant already has configuration. *All Firewalls* has two interfaces, `$eth-internet` and
`$eth-local`, whose default ports are `ethernet1/3` and `ethernet1/4`. It also has zones, a logical router
and a legacy virtual router that use those interfaces, and an attached Auto-VPN snippet. *Global* has
several predefined snippets attached.

When a Panorama template uses `ethernet1/3` or `ethernet1/4`, the import cannot give those ports to the
migrated interfaces:

```
snippet -> <template> -> object-variable -> interface -> ethernet -> $ethernet1-3 -> default-value 'ethernet1/3'is already in use
snippet -> <template> -> object-variable -> interface -> ethernet -> $ethernet1-3 -> default-value invalid. Discarding.
```

The imported firewalls then lack those interfaces. Nothing flags this beyond that line in the load
results, and a push would delete the interfaces on the devices.

## What it does

Only in *All Firewalls* (`ngfw-shared`), and in *Global* (`All`) with `--global`. Each step runs before the
step it references:

1. Delete every item the folder owns itself (not through a snippet): rules, logical and virtual routers,
   zones, interfaces, profiles, objects and variables.
2. If a zone is still referenced, keep it and remove its interfaces.
3. If an interface still cannot be deleted, clear its default port. This frees `ethernet1/x` for the import.
4. Detach every predefined or readonly snippet, one at a time. Snippets you created are never touched.

Before the first change, every item touched is saved to a JSON backup. Changes stay in the candidate
configuration: nothing is pushed.

## What SCM will not let go

These results come from a fresh tenant. The script reports them as `kept`, not as failures:

| Item | Why |
|---|---|
| zone `internet` (All Firewalls) | The Web Security *outbound zone* setting references it. The script empties the zone of interfaces. Clear that setting in the UI and the next run deletes the zone |
| tags `Sanctioned`, `Tolerated` (Global) | The predefined snippet `Gen-AI-Best-Practice` references them |
| snippets `default` / *Global-Default* and `Web-Security-Default` / *Internet-Security-Default* on Global | Predefined snippets attached nowhere (the VM templates, `DNS-Best-Practice`, `Internet-Access-Best-Practice`) reference their content, and predefined snippets cannot be deleted |
| the tenant's certificates (Root CA, Forward-Trust/UnTrust CAs, cookie CAs, SAML) | "Deleting default certificates is not allowed". The script does not attempt it |
| device settings (DNS, NTP, service routes, admin roles…) | Not exposed to a standard service account |

## Use

Python 3.9 or later, and `requests`.

```bash
pip install -r requirements.txt
cp .env.example .env        # fill in a service account of the tenant

python scm_preclean.py                    # plan: what would change
python scm_preclean.py --apply            # clean All Firewalls
python scm_preclean.py --apply --global   # also clean Global
```

Plan output on a fresh tenant:

```
tenant TSG ...1234
6 change(s) in All Firewalls:
  - [All Firewalls] delete logical-routers `default`
  - [All Firewalls] delete virtual-routers `default`
  - [All Firewalls] delete zones `local`
  - [All Firewalls] delete zones `internet`
  ...
  - [All Firewalls] detach snippet `Auto-VPN-Default-Snippet`
plan only: rerun with --apply
```

| Option | Meaning |
|---|---|
| `--apply` | Make the changes. Without it, the run only plans |
| `--global` | Also clean Global |
| `--env-prefix SCM_TEST` | Read `SCM_TEST_TSG_ID`, `SCM_TEST_CLIENT_ID` and `SCM_TEST_CLIENT_SECRET`, to reach a second tenant from the same `.env` |
| `--env-file PATH` | Read credentials from this `KEY=value` file (default `.env`). Environment variables take precedence |
| `--backup-dir DIR` | Where the JSON backup goes (default: the current directory) |

The exit code is 0 when nothing is left to remove other than what SCM keeps, and 1 otherwise.

The service account needs a role that can read and write the configuration of *All Firewalls* and *Global*
(for example *Superuser* on the tenant).

## Suggested order for a Panorama migration

1. Take a snapshot of the fresh tenant in SCM, so you can return to it.
2. Run `scm_preclean.py`, read the plan, then run it with `--apply` (and `--global` if you want).
3. Run the Panorama import.
4. In the load results, look for `already in use … Discarding` lines. After this clean there should be none
   for `ethernet1/3` or `ethernet1/4`.

## Tests

```bash
pip install pytest
pytest -q
```

The tests run offline against a fake tenant. They cover the planning rules (items a folder owns versus
items from a snippet, rulebase markers, which snippets get detached) and how referenced zones and
interfaces are handled.

## License

[Apache-2.0](LICENSE).
