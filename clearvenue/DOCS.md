# ClearVenue

Run one or more ClearSignage screens inside Home Assistant. Each screen is a complete,
independent signage player — its own content library, its own playlists, its own PIN —
and is managed on itself, the same way a Raspberry Pi appliance is.

This machine is also your **venue**: the one place that holds a connection to something
like your till, reads the prices from it, and sends them out to the screens that show
them. Screens never hold that connection themselves — they receive prices, never access.
It is also where this building's **occupancy** is counted and kept.

## Release channels

The repository offers ClearVenue, ClearVenue Beta and ClearVenue Dev. ClearVenue is for a
venue in service; Beta is the next release early; Dev is work in progress. Each is a
separate app with its own data, and switching apps does not copy venue data. Run only one
at a time on a machine: they use host networking and the same ports.

## Installing

1. **Add the registry credentials.** This app is a prebuilt image in a private registry,
   so Home Assistant needs a login before it can pull it. Add `ghcr.io` with the username
   and token you were given, under the Docker registries settings in the app store's ⋮
   menu. Do this *first*: without it the install fails at the pull with an
   authentication error, which reads like a broken repository rather than a missing
   credential.
2. Settings → Apps → Install app → ⋮ → **Repositories**, and add this repository's URL.
3. Install **ClearVenue** and start it.
4. Open it from the sidebar. The first screen is added from that page.

The credential is read access only, and it is stored by Home Assistant rather than in
this repository — so rotating it is a change on each install and nothing here.

## Configuration

| Option | Default | What it is |
|---|---|---|
| `host_ip` | *(blank)* | The address other screens use to reach this machine. Blank means the app works it out, preferring an ordinary local address over a VPN one. |
| `log_level` | `info` | |

**Set `host_ip` yourself if this machine runs a VPN.** Tailscale, ZeroTier and the like give
the machine an address that is not on your own network. Screens told to reach the venue there
cannot, and *nothing looks wrong*: the panel opens, the pages load, and only syncing quietly
never happens. The app prefers a local address and logs which one it chose and why, but you
know which network your screens are on and this is where you say so. It is also shown on the
**Screens** page.

## Reaching ClearVenue from outside the building

ClearVenue can be published on the internet through a reverse proxy — a Cloudflare tunnel,
Nginx Proxy Manager or similar — without publishing Home Assistant itself.

1. **Point the proxy at port `8098`** on this machine, and give it an https address of its own,
   such as `https://venue.example.com`. Only port `8098`: this app's other ports, Home
   Assistant's `8123` and each screen's `810N` are for inside the building, and answer anything
   a proxy sends them with *"This address is for inside the building"* — which is how a wrong
   port shows itself.

   With Nginx Proxy Manager: point your name's `A` record at your public address, have your
   router forward port `443` (and `80`, if the certificate is requested over HTTP) to the
   machine running the proxy, then add a proxy host for the name with scheme `http`, this
   machine's network address and port `8098`, and request an SSL certificate for it.
2. **Set `public_url`** in this app's Configuration tab to that address, and restart the app.
3. **Turn on Microsoft sign-in.** Open ClearVenue from the sidebar, then **Settings → Change how
   people sign in**. Fill in your Microsoft app registration, using the redirect address the
   page shows, save, and press **Test & enable Microsoft**. You finish signing in on the public
   address and become its first administrator; invite anybody else from the same page.

From outside the building, only Microsoft accounts you have made administrators can sign in,
and once signed in they manage ClearVenue and open its screens as they would from the sidebar,
without a screen's PIN. Kitchen and other staff screens, and anything else ClearVenue trusts
because it is on your own network, are not available there and stay on your own network; your
till's instant updates and your cameras still reach it. Inside the building nothing changes:
the sidebar works as before.

## Reaching a screen

Three ways, and they are not equivalent:

- **From the Home Assistant sidebar.** Home Assistant has already signed you in, so this
  is the path that can change what is on a screen, and it never asks for the screen's PIN.
  It also works from outside your home through Nabu Casa, with no port forwarding. Until you
  set a screen PIN in Settings, each new screen gets its own, shown on its card — that is the
  PIN to type on the screen's own address.
- **`http://<host>:810N`** — how *other screens* find and sync with this one, and the
  address to put a screen's `/display` on a dashboard (see below). Not somewhere to go
  looking for settings.

**There are no `http://<name>.local` addresses here**, and that is deliberate rather than
missing. A friendly name has to answer on port 80, and Home Assistant itself uses port 80 —
so the name would resolve and land you on Home Assistant's own login page rather than the
screen, which is more confusing than having no name at all. (They also never work when your
screens are on a different network from this machine, whatever we do.) An appliance in a
cupboard still publishes them; use the sidebar or the `:810N` address here.

## Backups

Home Assistant backs this app up with everything else, and there is nothing to set up. Two
things are worth knowing.

**What it takes is a copy, not the live database.** This venue writes its state as several
files at once, and copying those while a kitchen is pressing buttons produces a database that
is missing the last few hours or will not open at all. So the app hands Home Assistant a clean
copy whenever a backup runs, and restores from that copy by itself the first time it starts
afterwards. You do not have to do anything for either half.

**Check it has actually done it.** The *If something goes wrong* block on the venue's Settings
page says when this venue last gave Home Assistant a copy it could use — and says so loudly if
it never has. That is worth a glance on a quiet afternoon, because a backup that contains no
venue data looks exactly like one that worked, right up until you need it.

On a venue nobody has used yet it will say *never*, and that is correct rather than a
problem: there is nothing in this venue to copy. It changes the first time a backup runs
after you have put something in.

You can also take a copy yourself from that same block, at any time, without waiting for a
backup.

## What is different from a Raspberry Pi

This runs the same ClearSignage as an appliance, so content, playlists, schedules,
designs, apps and peer sync all behave identically. What is absent is absent because the
capability genuinely is not here, not because it was left out:

- **No display output.** A Pi drives an HDMI screen. Here the *browser* is the screen —
  put a screen's display on a dashboard, or open it full-screen on a wall tablet.
- **No Wi-Fi, network or reboot settings.** Those belong to Home Assistant and the
  machine it runs on. Restart the app from Home Assistant instead.
- **No software updates from inside a screen.** Home Assistant updates this app; an
  in-place update would be thrown away the next time it restarts.
- **Screens on your walls update from here.** A Raspberry Pi or other screen that joined
  this venue takes its software from this app: update the app in Home Assistant, and the
  screens joined to it follow by themselves at a quiet moment — while their schedule has the
  display turned off, or just after they are switched on. Until then a screen's own page
  says an update is waiting, and **Update software** there installs it straight away. They
  check the update is genuine before installing it, exactly as they would one from the
  internet. The venue's **About** page shows the screen software it carries.
- **No "sold out" / booking taps from the screen itself.** On an appliance those are
  authorised by *standing at the panel*, which a hosted screen has no way to check. Here
  they are authorised by your Home Assistant login instead — so use the sidebar when you
  want to change something.

## Putting a screen on a dashboard

Add a **Webpage** card and point it at that screen's display. The fleet page shows each
screen's address ready to paste — open ClearVenue from the sidebar and copy it from the
"Put on a dashboard" column:

```yaml
type: iframe
url: http://192.168.1.50:8101/display?playlist_only=1
aspect_ratio: 16x9
```

Two things about that URL. `?playlist_only=1`, because without it a composed screen tries
to render itself inside itself. And `8101` is screen 1 — screen 2 is `8102`, and so on.

**Do not use the address in your own address bar.** That one is an ingress path, and an
ingress path belongs to *your Home Assistant session* rather than to the screen. Anyone
else who opens the dashboard — most importantly a wall tablet signed in as a different
user — gets **401** from it, which looks exactly like a broken screen. The display
address above needs no sign-in at all: a screen's display carries no controls and no
secrets, which is what makes it safe to hand out.

### If your Home Assistant is reached over HTTPS

A browser will not embed `http://` content in a page served over `https://`, so on a Nabu
Casa install — or any Home Assistant behind TLS — the card above shows an empty box. Use
the ingress path there instead, and accept that it only works for the person signed in:

```yaml
type: iframe
url: /api/hassio_ingress/<token>/i/1/display?playlist_only=1
aspect_ratio: 16x9
```

The token changes; get it by opening the screen from the sidebar, copying the URL from the
address bar, and appending `display?playlist_only=1`.

For a whole screen on the wall, put one card in a **Panel** view (`type: panel` — a
Sections view, the default, is not what you want here):

```yaml
views:
  - title: Lobby
    type: panel
    cards:
      - type: iframe
        url: http://192.168.1.50:8101/display?playlist_only=1
        aspect_ratio: 16x9
        hide_background: true
```

*Checked against Home Assistant 2026.7.*

## Your till, and other live data

Open **Till** from the Screens page to connect your point-of-sale account. You sign in to
the provider once, in your own browser; what comes back is stored here and refreshed
automatically, and your screens are sent the prices only.

**Your Home Assistant backups will contain that connection — and more.** A backup includes
this app's data, and many people sync backups to cloud storage. Alongside the till it carries
this venue's **sign-in PIN** and this machine's **identity key**, which is what your screens
trust when they sync. The short version: *a backup of this venue is as sensitive as the venue
itself*, and somebody holding one holds everything except your Home Assistant password.

Treat one the way you would treat the password to the till account. If a backup is ever shared
or exposed: disconnect the till on the Till page — that revokes what the backup contains — and
change the venue's PIN in Settings.

You can also add screens that are not run by this machine. **Other screens** on the Screens
page finds ClearSignage screens elsewhere on your network and joins them to this venue, so
they receive its prices too. Joining is always explicit — nothing is enrolled by being
discovered — and letting one go afterwards affects only that screen.

## Counting how busy a space is

Open **Occupancy** from the Screens page. Add a space — a room you think of as one place —
say how many it holds, and add a camera at each door people come in through. Adding a camera
gives you a code to paste into it once; after that the camera counts on its own and this
machine keeps the totals and the hour-by-hour history.

**The camera sends numbers, never pictures.** No image is stored anywhere, there is no face
recognition and nothing follows anybody between cameras. The only picture that ever leaves a
camera is the single frame you drag the doorway line across while aiming it, and that is
shown to you and then dropped.

Two things worth knowing before you start:

- **One camera watches one doorway.** Two cameras that can both see the same door will count
  everybody through it twice, and nothing here can tell that apart from a genuinely busy pair
  of doors.
- **Counting drifts.** People walk through in pairs and deliveries block the view. A space
  can start counting again each night, and you can always set the number to what you have
  just counted by hand.

Any screen this venue has joined can show it: add a **How busy it is** board and pick the
space. Every number on that board is optional, so it can be one large figure in a window or
the lot on a back-office screen.

## Your content

Everything lives in the app's `/data` — one directory per screen, plus this venue's own
connections. Removing a screen from the fleet page leaves its content on disk, so removing
one by mistake is recoverable; uninstalling the app is not.
