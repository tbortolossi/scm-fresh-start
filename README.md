# scm-preclean

[![CI](https://github.com/tbortolossi/scm-preclean/actions/workflows/ci.yml/badge.svg)](https://github.com/tbortolossi/scm-preclean/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-green)](LICENSE)

**Get a Strata Cloud Manager tenant with no configuration of its own, the way a Panorama is right after
installation.** No interfaces, variables, zones, routers or snippets already set up: what you migrate from
Panorama is the only configuration on the tenant.

> Personal project, not a Palo Alto Networks product and not supported by Palo Alto Networks. It uses the
> public [SCM configuration API](https://pan.dev/scm/docs/home/). Try it on a test tenant first.

## Why: SCM never starts empty

A new Panorama has no templates, no device groups, no interfaces and no zones. You import or build your
configuration and nothing else is there.

A new SCM tenant is different. It comes with a default NGFW configuration, and there is no option to get
one without it:

| Where | What is already configured |
|---|---|
| *All Firewalls* | Interfaces `$eth-internet` (port `ethernet1/3`) and `$eth-local` (port `ethernet1/4`), with their variables |
| | Zones `internet` and `local` that use those interfaces |
| | A logical router and a legacy virtual router named `default` |
| | The snippet `Auto-VPN-Default-Snippet`, attached |
| | The Internet Security setting *Outbound Zone* = `internet` |
| *Global* | Several predefined snippets attached (*Global-Default*, *Internet-Security-Default*, *GlobalProtect-Default*…), with their profiles, application filters and tags |

All Firewalls applies to every firewall on the tenant, so each firewall you import inherits this
configuration on top of its own.

### What goes wrong in a Panorama import

When a Panorama template uses `ethernet1/3` or `ethernet1/4`, the import cannot give those ports to the
migrated interfaces, because the tenant's defaults already hold them. The load results show:

```
snippet -> <template> -> object-variable -> interface -> ethernet -> $ethernet1-3 -> default-value 'ethernet1/3'is already in use
snippet -> <template> -> object-variable -> interface -> ethernet -> $ethernet1-3 -> default-value invalid. Discarding.
```

The imported firewalls then lack those interfaces. Nothing else flags it, and **a push would delete the
interfaces on the devices**, with their addresses, zones, routing and VPNs.

### Why it runs after the import

Cleaning the tenant before the import does not help. The import reloads the tenant's full default
configuration before it adds yours: everything removed beforehand comes back. So the order is:

1. import from Panorama;
2. run `scm-preclean` to remove the defaults and give the migrated interfaces their ports back;
3. check, then push.

## What it removes

Only the tenant's default configuration, in *All Firewalls* and (with `--global`) in *Global*.

| Removed | Details |
|---|---|
| Interfaces and variables | `$eth-internet`, `$eth-local` and every other interface or variable the folder owns |
| Zones | `internet`, `local` and any other zone the folder owns. A zone that something still references is kept, but emptied of its interfaces |
| Routers | the logical router and the virtual router `default` |
| Snippets | the predefined snippets attached to the folder (`Auto-VPN-Default-Snippet` on All Firewalls, the default snippets on Global) are **detached**. SCM does not let anyone delete them |
| Rules, profiles, objects | security and NAT rules, security profiles and profile groups, addresses, services, tags… owned by the folder |
| Internet Security zones | *Outbound Zone* set to `any` (with `--ui-api`; otherwise the script tells you to do it in the UI) |

It also **repairs the import**: in the imported snippets, each interface variable named `$ethernetX-Y` that
lost its port gets `ethernetX/Y` back. Subinterfaces follow their parent.

### What it does not touch

- **Your migrated configuration.** Snippets you created, including the ones the Panorama import created
  from your templates and device groups, are never deleted or detached. Only the port repair above
  edits them.
- **Your firewalls.** Every change stays in the SCM candidate configuration. Nothing is pushed: you
  decide when to push.
- **What SCM refuses to remove**, even from its own UI: the tenant's certificates, device settings (DNS,
  NTP, admin roles…) and a few items pinned by predefined snippets. The script lists them as `kept`, with
  the reason. See [What pins Global](#what-pins-global-and---deep).

Before the first change, the script saves every item it touches to a JSON file
(`scm-preclean-<last 4 digits of the TSG ID>-<date>.json`), so you know exactly what was there.

## Step by step

You do not need to know Python. You need a terminal, about 15 minutes, and admin rights on the SCM tenant.

### 1. Install

Install [Python](https://www.python.org/downloads/) 3.9 or later if you do not have it. Check with
`python3 --version` (Windows: `py --version`).

Download this repository (**Code → Download ZIP** on GitHub, then unzip it), open a terminal in its
folder and run:

```bash
# Linux / macOS
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

```powershell
# Windows (PowerShell)
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

In each new terminal, run the `activate` line again before you use the script.

### 2. Create the credentials

The script logs in to SCM as a **service account**: a technical user with a Client ID and a Client
Secret instead of a login and password. It must belong to the tenant you want to clean.

1. In SCM, switch to the tenant to clean (top of the screen), then open **Identity & Access**
   (under *Settings* or *Common Services*, depending on your SCM version).
2. **Add Identity**, identity type **Service Account**, name it for example `scm-preclean`.
3. Copy the **Client ID** and the **Client Secret** somewhere safe. **The secret is shown only once**; if
   you lose it, create a new service account.
4. Give the service account a role that can change the configuration of *All Firewalls* and *Global*:
   **Superuser** on this tenant. Assign it on the tenant you clean, not on a parent tenant.
5. Find the **TSG ID**, the tenant's number. It is inside the Client ID:
   `scm-preclean@`**`1234567890`**`.iam.panserviceaccount.com` → TSG ID `1234567890`.

Now give them to the script. In the repository folder, copy the example file:

```bash
cp .env.example .env        # Windows: copy .env.example .env
```

Open `.env` in any text editor and fill in the three lines, without quotes or spaces:

```
SCM_TSG_ID=1234567890
SCM_CLIENT_ID=scm-preclean@1234567890.iam.panserviceaccount.com
SCM_CLIENT_SECRET=paste-the-secret-here
```

`.env` holds a password to your tenant: **do not send it, commit it or share it.** It is excluded from git
by `.gitignore`. When the migration is done, delete the service account in SCM.

### 3. Run it

Always start with a plan. It changes nothing and lists what it would do:

```bash
python scm_preclean.py
```

```
tenant TSG ...7890
10 change(s) in All Firewalls:
  - [All Firewalls] Internet Security zones: inbound any -> any, outbound internet -> any
  - [All Firewalls] delete logical-routers `default`
  - [All Firewalls] delete virtual-routers `default`
  - [All Firewalls] delete zones `local`
  - [All Firewalls] delete zones `internet`
  ...
  - [All Firewalls] detach snippet `Auto-VPN-Default-Snippet`
  - [TPL-SITE] set $ethernet1-3 default port -> ethernet1/3
  - [TPL-HA] set $ethernet1-4 default port -> ethernet1/4
plan only: rerun with --apply
```

Read it. Lines in `[All Firewalls]` or `[Global]` remove defaults; lines with your template names
(`[TPL-SITE]` above) give your interfaces their ports back. Lines starting with `=` are items SCM does not
let go, with the reason. If a line touches something you want to keep, stop there.

Then apply:

```bash
python scm_preclean.py --apply            # All Firewalls only
python scm_preclean.py --apply --global   # All Firewalls and Global
```

The script prints where the JSON backup is, each change, and a summary: how many items were removed,
how many were kept, and anything still left.

**Then refresh the SCM page in your browser** (F5). The web UI does not show changes made through the API
until you reload it: without a refresh you still see the old interfaces, zones and snippets, and a change
you save from that stale page can put them back.

If the output says `referenced by swg/general-settings/outbound-zone` for the zone `internet`, do this in
the UI, then run the script again: **Internet Security → General → General Settings**, scope *All Firewalls*,
**Outbound Zone** = `any`.

### 4. Check before the first push

Refresh the SCM page first if you have not done it since the run. Then, for each firewall, compare its interface list in SCM with Panorama: `ethernet1/3` and `ethernet1/4` must
be back, with their addresses and zones. This was verified on a lab import: after the script, the firewalls
had both interfaces back, with their addresses, zones, virtual routers and IKE gateway references.

## Suggested order for a Panorama migration

1. Take a snapshot of the tenant in SCM, so you can return to it.
2. Run the Panorama import. In the load results, note the `already in use … Discarding` lines.
3. Run `scm_preclean.py`, read the plan, then run it with `--apply` (and `--global`, or `--deep` for a bare
   Global).
4. Refresh the SCM page in your browser.
5. Check each firewall's interface list against Panorama.
6. Push.

## Options

| Option | Meaning |
|---|---|
| `--apply` | Make the changes. Without it, the run only plans |
| `--global` | Also clean Global |
| `--deep` | Implies `--global`. Also edit the predefined snippets that keep Global from being emptied. See below |
| `--ui-api URL` | Set the Internet Security zones through the SCM UI's backend, instead of by hand. See below |
| `--env-prefix SCM_TEST` | Read `SCM_TEST_TSG_ID`, `SCM_TEST_CLIENT_ID` and `SCM_TEST_CLIENT_SECRET`, to reach a second tenant from the same `.env` |
| `--env-file PATH` | Read credentials from this file (default `.env`). Environment variables take precedence |
| `--backup-dir DIR` | Where the JSON backup goes (default: the current folder) |

The exit code is 0 when nothing is left to remove other than what SCM keeps, and 1 otherwise.

### `--ui-api`

The Internet Security *Outbound Zone* is not in the public API. `--ui-api` sets it through the backend the
SCM web UI itself uses, which is not a documented API and may change. Its address is per tenant and region.
To find it: open SCM in your browser, press F12, open the **Network** tab, reload, and look for requests to a
host like `https://paas-N.prod.<region>.panorama.paloaltonetworks.com`. Put it in `.env` as
`SCM_UI_API=https://...` or pass it as `--ui-api https://...`.

If this is too much, skip it and change the setting in the UI (step 3 above).

## What pins Global, and `--deep`

These results come from a fresh tenant, and the SCM web UI refuses the same operations with the same
references, so this is SCM's own limit, not an API or role limit. Without `--deep`, the script reports them as
`kept`, not as failures:

| Item | Why |
|---|---|
| zone `internet` (All Firewalls) | The Internet Security setting `swg -> general-settings -> outbound-zone` references it, and the public API does not expose that setting. With `--ui-api`, the script sets the Inbound and Outbound Zones of All Firewalls to `any` first, and the zone deletes. Without it, the script empties the zone and prints the manual fix |
| tags `Sanctioned`, `Tolerated` (Global) | The predefined snippet `Gen-AI-Best-Practice` references them |
| snippets `default` / *Global-Default* and `Web-Security-Default` / *Internet-Security-Default* on Global | Predefined snippets attached nowhere reference their content, and predefined snippets cannot be deleted. *Global-Default*: its `best-practice` zone protection profile is used by the zones of the AWS/Azure/GCP/AIRS VM templates. *Internet-Security-Default*: its `web-security-default` profiles are used by `DNS-Best-Practice-pg`, its application filters by the VM templates' rules and by `Global Web Access-Allow/Block` (`Internet-Access-Best-Practice`), and its `Web Security Global` tag by those same two rules |
| the tenant's certificates (Root CA, Forward-Trust/UnTrust CAs, cookie CAs, SAML) | "Deleting default certificates is not allowed". The script does not attempt it |
| snippet `GlobalProtect-Default` on Global, when attached | Its `Local Users` authentication profile is used by the Mobile Users authentication setting `DEFAULT` (portal and gateway client auth). SCM refuses the detach with a bare 500 until that setting is gone |
| device settings (DNS, NTP, service routes, admin roles…) | Not exposed to a standard service account |

The blockers on Global are **predefined snippets attached nowhere**. They cannot be deleted, but their
objects can be edited. `--deep` makes the smallest edits that release Global, before any delete or detach:

| Blocker | Edit |
|---|---|
| zones of the VM templates, and All Firewalls' `internet` zone, use Global-Default's `best-practice` zone protection | the zone protection profile is removed from those zones. The `internet` zone's reference is the one SCM hides behind a bare `500 config connection returned error` |
| VM template rules use `All Web Applications` | the application becomes `any` |
| `DNS-Best-Practice-pg` uses the `web-security-default` profiles | it uses the predefined `best-practice` profiles |
| `Global Web Access-Allow` / `-Block` (Internet-Access-Best-Practice) use the `Web Security Global` tag and web application filters | those fields are removed |
| Mobile Users authentication setting `DEFAULT` uses GlobalProtect-Default's `Local Users` profile | the setting is deleted. **Mobile Users then has no client authentication**: add one before you deploy Mobile Users on this tenant |
| Gen-AI-Best-Practice filters are tagged `Sanctioned` / `Tolerated` | the tag is removed. When the Internet rule `Sanctioned Gen AI Access` pins the filter ("… is not a valid reference"), that rule is deleted first |

This changes Palo Alto's predefined templates in your tenant: the VM and best-practice snippets no longer
match what they ship with. Use `--deep` only on a tenant you want bare. The JSON backup records every
original object.

## Tests

```bash
pip install pytest
pytest -q
```

The tests run offline against a fake tenant. They cover the planning rules (items a folder owns versus
items from a snippet, rulebase markers, which snippets get detached, which interface variables get their port
back), how referenced zones and interfaces are handled, and the `--deep` edits.

## License

[Apache-2.0](LICENSE).
