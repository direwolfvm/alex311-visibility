# Push notifications

"Your request changed", on the phone, when the mirror learns of it.

## What is sent
After each ingest (four times a day), for every request an account **sent or
follows** whose status differs from the one that account was last told about,
one notification per registered device:

```json
{"aps": {"alert": {"title": "Noise Issues: closed",
                   "body": "Your request 26-00036546 at 500 N PITT ST is now closed."},
         "sound": "default", "thread-id": "26-00036546"},
 "case": "26-00036546",
 "url": "https://alex311visibility.me/r/26-00036546"}
```

`case` is what the app should open; `url` is the same record as a Universal
Link. The first time a link's request is seen, nothing is sent — that status
is the baseline. Watches (type / address / area) are not pushed; they are the
daily digest's job.

## For the app
After the person allows notifications, and again on every launch (tokens
change), with the session cookie:

| | |
|---|---|
| `POST /submit/api/devices` `{"token": "<hex APNs token>", "environment": "production" \| "sandbox"}` | Registers or refreshes. A development build must say `sandbox`; TestFlight and the App Store are `production`. Returns `{registered, environment, push_available}`. |
| `DELETE /submit/api/devices/{token}` | On sign-out. |

The app needs the Push Notifications capability (`aps-environment`
entitlement) and to handle a tap by opening `case`. A token belongs to one
account at a time: signing in as someone else on the same phone moves it.
Ten devices per account; the oldest fall off.

## Server configuration
All four, on the **ingest job** (it is the one that sends), or nothing is sent:

```bash
printf '%s' "$(cat AuthKey_XXXXXXXXXX.p8)" | gcloud secrets create alex311-apns-key --data-file=-
gcloud run jobs update alex311-ingest --region=$REGION \
    --update-secrets=APNS_KEY=alex311-apns-key:latest \
    --update-env-vars=APNS_KEY_ID=<key id>,APNS_TEAM_ID=<team id>,APNS_TOPIC=com.herbertindustries.Alex311-Reborn,SITE_ORIGIN=https://alex311visibility.me
```

and `APNS_*` on the dashboard service too if `push_available` should read true
at registration. A token Apple reports gone (`410`, `BadDeviceToken`,
`Unregistered`) is switched off, not retried.
