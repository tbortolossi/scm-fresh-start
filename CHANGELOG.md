# Changelog

## 0.2.0

- `--deep`: edit the predefined snippets that pin Global (zone protection, application filters, DNS
  profile group, Internet rules, Gen-AI tags) so that Global's snippets detach and its tags delete.
  Tested step by step on a fresh tenant.
- Zone `internet`: print the manual fix (Internet Security > General Settings > Outbound Zone = any).
  With it, All Firewalls and Global end up empty.

## 0.1.0

- First release: plan / `--apply` / `--global`, reference-ordered deletion, fallbacks for referenced
  zones (interfaces removed) and interfaces (default port cleared), per-snippet detach, JSON backup, and
  a documented list of what SCM refuses to remove.
