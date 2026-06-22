# UI translations

Each `<code>.json` file in this directory is one UI language. They are
**discovered dynamically** — drop a new file here and it appears in the
language selector on the next page load. No code change, no rebuild
(the `ui/` tree is served live).

`en.json` is the **source of truth and fallback**: English is always
loaded first, then the selected language is merged on top. Any key a
translation omits falls back to English, so **partial translations are
fine** — translate what you can and ship it.

## Add a language

1. Copy `en.json` to `<code>.json`, where `<code>` is the lowercase
   ISO 639-1 code (`es`, `de`, `fr`, `it`, `pt`, …).
2. Edit the `_meta` block:
   ```json
   "_meta": { "name": "Spanish", "nativeName": "Español", "code": "es", "flag": "🇪🇸" }
   ```
   - `name` — English name (for tooling)
   - `nativeName` — what the speaker sees in the selector
   - `code` — must match the filename
   - `flag` — emoji flag (optional)
3. Translate the **values** only. **Never change the keys** (the part
   left of the colon, e.g. `nav.dashboard`) — they bind to the UI.
4. Reload the page. Your language is in the selector.

## Conventions

- Keys are namespaced by area: `nav.*`, `status.*`, `common.*`,
  `dashboard.*`, `history.*`, `energy.*`, `registers.*`, `config.*`,
  `vmeters.*`. Keep new keys in the right namespace.
- Keep technical tokens untranslated where they are product names:
  `Modbus`, `MQTT`, `InfluxDB`, `kWh`.
- Use the `…` ellipsis character (not three dots) to match `en.json`.
- The file must be valid UTF-8 JSON. Validate with:
  `python3 -c "import json; json.load(open('es.json'))"`

## Add a new string to the UI

When you add UI text that should be translatable:

- **Static HTML** — add `data-i18n="area.key"` (or
  `data-i18n-placeholder` / `data-i18n-title`) to the element. Put it on
  a text-only element or a `<span>` so icons are preserved.
- **Dynamic JS** — wrap the string in `this.t('area.key', 'English fallback')`.
- Add the key + English text to `en.json`, then to the other languages
  (or leave them to fall back to English until translated).
