# Web Push Service Worker Spec (HFT-32)

This document is the contract between the backend Web Push dispatcher and the
frontend service worker. The backend sends the payload below; the frontend
service worker renders it as a notification. The service-worker file itself and
the opt-in UI are separate frontend work — this spec defines only the payload
shape and the expected `push` / `notificationclick` behavior.

## Subscription flow (frontend responsibilities)

1. `GET /api/alerts/vapid-public-key` → `{ "vapid_public_key": "<base64url>" }`.
   An empty string means Web Push is not configured for this deployment; the
   frontend should hide the opt-in control.
2. Register the service worker and call
   `registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey })`,
   where `applicationServerKey` is the VAPID public key converted from base64url
   to a `Uint8Array`.
3. `POST /api/alerts/subscriptions` with the native
   `PushSubscription.toJSON()` body (see below) → `201 Created`.
4. On unsubscribe, `DELETE /api/alerts/subscriptions` with
   `{ "endpoint": "<endpoint>" }` → `204 No Content`.

## Subscription request body

`POST /api/alerts/subscriptions` accepts the W3C `PushSubscription.toJSON()`
shape directly, so the browser object can be posted without reshaping:

```json
{
  "endpoint": "https://fcm.googleapis.com/fcm/send/abc123",
  "keys": {
    "p256dh": "BOrnIslXrUow2VAzKCUAE4sIbK00daEZ...",
    "auth": "k8JV6sjdbhAi1n3_LDBLvA"
  }
}
```

The server assigns `created_at`; clients do not send it. The `endpoint` is the
unique key, so re-subscribing the same browser is idempotent.

## Push payload (backend → service worker)

The backend `data` body is a JSON object with this exact shape:

```json
{
  "title_en": "Flood Alert – ORANGE",
  "title_th": "แจ้งเตือนน้ำท่วม – เฝ้าระวัง",
  "body_en": "Basin risk level raised to ORANGE. Valid: 2026-05-27 18:00 UTC.",
  "body_th": "ความเสี่ยงน้ำท่วมระดับเฝ้าระวัง ณ 2026-05-27 18:00 UTC",
  "url": "https://hatyai-flood-warning.vercel.app",
  "risk_level": "orange"
}
```

Field reference:

| Field        | Type   | Notes                                                            |
| ------------ | ------ | ---------------------------------------------------------------- |
| `title_en`   | string | English notification title.                                     |
| `title_th`   | string | Thai notification title.                                        |
| `body_en`    | string | English body; includes the forecast valid time in UTC.         |
| `body_th`    | string | Thai body; includes the forecast valid time in UTC.            |
| `url`        | string | URL to open on `notificationclick`.                             |
| `risk_level` | string | One of `green`, `yellow`, `orange`, `red`. Drives icon/color.   |

Notes:

- Only `orange` and `red` are ever broadcast. The dispatcher is edge-triggered:
  it fires on an upward risk transition and applies a cooldown that a further
  upward transition (e.g. `orange` → `red`) bypasses. The service worker does
  not need to deduplicate.
- Valid times are formatted as `YYYY-MM-DD HH:MM UTC`. When the forecast valid
  time is unknown the backend substitutes `N/A`.

## Expected service worker handlers

```js
self.addEventListener("push", (event) => {
  const data = event.data.json();
  // Pick locale from the client's language preference; default to Thai.
  const title = data.title_th;
  const options = {
    body: data.body_th,
    data: { url: data.url, risk_level: data.risk_level },
    // Choose icon/badge per data.risk_level (orange vs red).
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil(clients.openWindow(event.notification.data.url));
});
```

## Pruning (backend behavior)

When the push service returns `404 Not Found` or `410 Gone` for an endpoint,
the backend deletes that subscription from MongoDB. The frontend does not need
to take action; a browser that lost its subscription will re-subscribe through
the normal opt-in flow.
```
