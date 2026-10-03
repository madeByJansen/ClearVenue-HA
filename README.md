# ClearVenue — Home Assistant apps

ClearVenue runs your venue's signage and live data from the Home Assistant machine you
already have: screens, menus and prices from your till, the kitchen, and how busy your
spaces are. This repository is the Home Assistant app repository for it.

## Apps

| App | For |
| --- | --- |
| **ClearVenue** | Venues in service. Updated when a release is ready. |
| **ClearVenue Beta** | Trying the next release before it reaches ClearVenue. |
| **ClearVenue Dev** | Testing work in progress. Not for a venue in service. |

Each app is separate and keeps its own data: moving from one to another does not copy
anything across. Run **one** of them on a Home Assistant machine at a time — they use the
same network ports.

## Installing

1. **Add the registry login first.** The apps are prebuilt images in a private registry.
   In Home Assistant, open Settings → Apps → Install app → ⋮ → **Registries** and add
   `ghcr.io` with the username and token you were given. Without it the install fails with
   an authentication error.
2. Settings → Apps → Install app → ⋮ → **Repositories**, and add this repository's URL.
3. Install **ClearVenue** and start it.
4. Open it from the sidebar.

The app's **Documentation** tab covers configuration, backups, putting a screen on a
dashboard, and what differs from a screen on a Raspberry Pi.

## Updates

Home Assistant offers each new release as an update to the app. Screens that joined the
venue update themselves from it afterwards, at a quiet moment.

## Support

Questions and problems go to the person or company that set ClearVenue up for you.
