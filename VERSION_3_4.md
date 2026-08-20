# BeauHoroscope 3.4

Production update for the astrology Content Engine.

## Added

- Manual immediate publication of an astrology article.
- 1–6 automatic content publication slots per day.
- Preset and custom publication times.
- Topic/text/semantic duplicate protection via `content_history`.
- Per-slot idempotency using keys such as `content:2026-08-20:09:00`.
- SQLite unique index for active Content Engine slots.
- Automatic cleanup of duplicate queued Content Engine slots on startup and scheduler passes.
- Protection against repeated generation of the same slot after a successful publication.
- Improved mobile-oriented article formatting and duplicated-block validation.

## Recommended schedule examples

```text
1x: 15:00
2x: 09:00, 18:00
3x: 09:00, 15:00, 21:00
4x: 09:00, 13:00, 17:00, 21:00
```

Custom times may be supplied as comma-separated `HH:MM` values, up to six slots per day.

## Production files

- `horoscope_free_pipeline_v9_4_content_engine_fixed.py` — current production entrypoint.
- `requirements.txt` — runtime dependencies.
- `.env` — runtime secrets/configuration and must remain outside Git.
- `horoscopes.db` — runtime state and publication history and must remain outside Git.

## systemd

The production service runs:

```ini
ExecStart=/opt/horoscope-bot/venv/bin/python3 /opt/horoscope-bot/main.py
```

## Validation

```bash
python3 -m py_compile horoscope_free_pipeline_v9_4_content_engine_fixed.py
```
