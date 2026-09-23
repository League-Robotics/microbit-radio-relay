# AGENTS.md — this repo publishes docs to the League Robotics hub

Documentation for this repo lives in the **GitHub wiki**:
<https://github.com/League-Robotics/microbit-radio-relay/wiki>

The League Robotics docs hub *clones the wiki* and publishes it at
<https://league-robotics.github.io/subsystems/microbit-radio-relay/>. The hub never writes back here —
edit wiki pages directly.

## What to do

When you learn something a future agent will need, edit or add a page in the wiki:

- Each wiki `*.md` page is one published page and needs `title:` and `blurb:` front matter.
- `docs/wiki/_subsystem.yml` (the ONLY file here) holds this subsystem's metadata
  (name / title / blurb). Changes to it auto-ping the hub.
- Add an `updated:` date to a page's front matter when you change it.
- Edit and push the wiki, and stop there. The hub picks up wiki changes by itself —
  do not trigger or touch its rebuild.

## How to understand what to do

The complete contract — file formats, the notify workflow, how to register — is the
authoritative spec at **<https://league-robotics.github.io/publishing/>**. Start there.
