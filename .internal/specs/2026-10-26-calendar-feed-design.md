# Calendar feed (approved 2026-10-26)

Goal: bookings appear in Google Calendar without Google Cloud setup.

- `GET /calendar.ics?token=T&business=ID` returns an iCalendar feed (`text/calendar`). Subscribe in Google Calendar via Other calendars > From URL.
- Token T = HMAC-SHA256(DASHBOARD_TOKEN, "calendar:" + business id), first 32 hex chars. It is not the dashboard token and is different per business. Wrong or missing token, or no DASHBOARD_TOKEN set: 404.
- One event per appointment, UID `<confirmation>@receptionist`. Title: "<service> - <client>". Description: phone, confirmation, notes. Location: address and town for house calls only.
- Length: the booking window (one hour for the med spa, the arrival window for house calls). Times are emitted in UTC.
- Cancelled appointments are emitted with STATUS:CANCELLED for 30 days so subscribers drop them; moved ones keep the UID.
- `python -m receptionist calendar-url --base-url URL` prints the link (pipe it to pbcopy; do not paste it in chat).
- One-way and delayed: Google refreshes subscribed feeds every several hours. Hand-made calendar events do not block availability.
- Tests: format, folding and escaping, moved and cancelled, token check, per-business tokens.
