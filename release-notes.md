# EDLS 1.3.0

Tools that need admin rights, an update check, and a round of fixes from QA.

## Download

**`EDLS-Setup-1.3.0.exe`** (under Assets below)

- Installs over 1.2.1 and keeps your settings. Setup closes EDLS for you; Steam, the game and your tools stay open.
- 1.2.1 can't tell you about new versions, so this one is a manual download. From 1.3.0 on, EDLS tells you itself.
- SHA256: `TODO`
- [VirusTotal Analysis for EDLS 1.3.0](TODO)
- You still need Steam, Elite: Dangerous and min-ed-launcher set up first, as the README describes

## What's new

- **Tools that need admin rights.** EDLS can now start EDHM-UI and other tools that ask for administrator rights: Windows shows its usual UAC prompt. If EDLS has to close one later, Windows asks once more, one prompt for all of them. Don't want the prompts? Turn off "Ask for admin rights when a tool needs them" in Settings.
- **Update check.** At startup EDLS asks GitHub whether there's a newer version. If there is, an "update" link appears next to the version number and takes you to the release page. EDLS never downloads or installs anything itself. Turn it off in Settings.
- **STAY OPEN.** Changed your mind during the 5-second close countdown? LAUNCH turns into STAY OPEN: click it and EDLS stays put.
- **Elite Studio** joins the default tools.
- **Only your tools start ticked.** On first run, and after RESTORE DEFAULTS, only the tools EDLS finds on your PC are enabled, so no more wall of "not found" warnings. Upgrading? Elite Studio is added unticked unless you have it, and your other choices stay as they were.
- **Settings.** Hover help on the toggles (look for the "i"), and the Light mode toggle previews the theme as you flip it (Cancel puts it back).
- Cards for tools you've disabled now sit after the enabled ones.

## Other fixes

- Odyssey Materials Helper now starts through its own launcher, which updates it first.
- Tools that run as several processes (EDHM-UI and other Electron apps) now close properly, instead of staying open while their card flips back to Online.
- If the game exits while Settings or another EDLS window is open, EDLS no longer closes itself behind it.
- The activity log stays put while you're scrolled up reading it.
- A renamed SrvSurvey is still found after restarting EDLS.
- CMDR and tool names show exactly as typed, even ones that look like HTML.

## Known issues

- Some antivirus tools distrust unsigned apps that start and close other programs. If yours complains, check the VirusTotal link above.
- The UAC prompt for an admin tool can pop up just as the game starts, and may knock a fullscreen game out of focus.
- After you answer Yes to that prompt, the tool's card can show Failed for a few seconds before EDLS spots it.
- If the game restarts mid-session (a relog, or min-ed-launcher restarting it), EDLS takes that as the end of the session and closes your tools. Press STAY OPEN during the countdown, then LAUNCH: EDLS attaches to the running game and starts your tools again.
- On a slow cold start (Steam starting, updates, logins) the 60-second wait for the game can run out. Press LAUNCH again.

## Found a bug?

Please submit it via the issues tab, and attach `launcher.log` (Settings → **OPEN LOG FOLDER**). Be sure to also include your operating system and hardware specs.
