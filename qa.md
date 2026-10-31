# EDLS — QA Test Plan

This is the manual test plan for **EDLS (Elite: Dangerous Launch Suite)**. EDLS is a
Windows launcher. One click starts Steam, then Elite: Dangerous, then your companion
apps (EDMC, SrvSurvey, VoiceAttack, …). It watches the game while you play and closes
the companion apps when the game exits.

The automated check (`python selftest.py`) only proves the window can be built without
errors. It can't see the screen and it never starts real programs. Everything in this
document needs a person.

Written against version **1.3.0** (the badge in the bottom-left corner of the window).
If your badge shows a different version, some details may have changed. Note the
version in every report.

**Contents**

1. [How to use this document](#1-how-to-use-this-document)
2. [Ground rules](#2-ground-rules)
3. [Setup](#3-setup)
4. [Test harness](#4-test-harness)
5. [Quick reference](#5-quick-reference)
6. [Smoke test](#6-smoke-test)
7. [Regression checks](#7-regression-checks)
8. [Detailed tests](#8-detailed-tests)
9. [Interaction sequences](#9-interaction-sequences)
10. [End-to-end journeys](#10-end-to-end-journeys)
11. [Environments](#11-environments)
12. [Known risk areas](#12-known-risk-areas)
13. [Reporting bugs](#13-reporting-bugs)
14. [Sign-off](#14-sign-off)

---

## 1. How to use this document

Every test has these columns:

| Column | Meaning |
| --- | --- |
| **ID** | Quote it in your report, e.g. `LS-04`. |
| **Pri** | **P1** must pass before release. **P2** should pass. **P3** polish or "good to know". |
| **Env** | **R** needs real Steam and the real game. **H** uses the test harness (fake game, [§4](#4-test-harness)). **A** works either way and needs no game. |
| **Steps** | What to do. `→` means "then". |
| **Expected** | What should happen. **"Record"** means there's no agreed right answer yet: describe what you see. It passes if nothing crashes, unless it seems wrong to you as a player. |

**R-nn** links point to [§12 Known risk areas](#12-known-risk-areas). Those are things a
code review suggests may be broken. Please confirm or rule out each one.

Suggested order for a first pass: Setup → Smoke test → Regression checks → every P1 →
P2 → P3. Report every failure ([§13](#13-reporting-bugs)). Also report anything that
confused you, even if the test technically passed.

---

## 2. Ground rules

1. **EDLS must never close Steam or Elite: Dangerous.** Force-closing the game breaks
   Frontier's Terms of Service. If Steam or the game closes and you didn't close it
   yourself, that's a **Blocker**. Stop, save `launcher.log` ([§3.3](#33-where-edls-keeps-its-files)),
   and report it straight away.
2. **Never end the real game from Task Manager.** Quit from the in-game menu. When a
   test needs the game to "exit" on cue, use the fake game ([§4](#4-test-harness)).
3. **EDLS asks companion apps to close, then forces them.** Each one is asked first, the
   way its own X button would. Anything still running 3 s later is closed by force: no
   "save changes?" prompt, no clean exit. Before teardown tests on your real tools, back
   up their data or use dummy apps.
4. Don't run the fake game and the real game at the same time.
5. Back up `%LOCALAPPDATA%\EDLS` before you start, and restore it when you're done.
6. Close EDLS before you hand-edit `settings.json`. EDLS overwrites the file whenever it
   saves.

---

## 3. Setup

### 3.1 What you need

- Windows 10 (22H2) or Windows 11, 64-bit.
- Python 3.11 or newer (`python --version`), only for running from source (§3.2). The
  installed build (§3.5) needs no Python.
- Steam, installed and logged in, with Elite: Dangerous in the library. The game should
  already launch through **min-ed-launcher** (see the README's Prerequisites). Epic and
  Frontier-store licences are out of scope, because EDLS always launches through Steam.
  If that's all you have, do the harness tests and say so in your report.
- Some of the 13 supported companion apps installed (optional, but needed for §8.15).
- Recommended: **Process Explorer** (free, from Microsoft Sysinternals). It shows which
  program started which, and is used in §8.14.

### 3.2 Install and run

```powershell
git clone https://github.com/coyoteware/EDLS.git
cd EDLS
git checkout <branch you were asked to test>
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python main.py
```

Run EDLS from a normal PowerShell or Terminal window unless a test says otherwise. Keep
that window visible, because errors (tracebacks) are printed there.

Record the exact build you're testing with `git rev-parse --short HEAD`.

### 3.3 Where EDLS keeps its files

| File | What it is |
| --- | --- |
| `%LOCALAPPDATA%\EDLS\settings.json` | All settings. Paste `%LOCALAPPDATA%\EDLS` into Explorer's address bar to get there. |
| `%LOCALAPPDATA%\EDLS\launcher.log` | Permanent log. Rolls over at about 500 KB into `launcher.log.1` … `.3`. |

Settings → **OPEN LOG FOLDER** also opens this folder.

Reset recipes (close EDLS first):

- **Back up:** copy the whole `EDLS` folder somewhere safe.
- **Back to first-run:** delete `settings.json`.
- **Full reset:** delete the whole `EDLS` folder.
- **Restore:** copy your backup back over the folder.

### 3.4 Handy tools

- **Task Manager → Details tab.** Lists every running program by file name. That's
  exactly what EDLS looks at.
- **Process Explorer.** Shows parent/child relationships. Highlights "jobs" (§8.14).
- **Win+Shift+S** for screenshots. **Win+Alt+R** records video (Xbox Game Bar).
- **Notepad** for editing `settings.json`. Keep the encoding as plain **UTF-8** unless a
  test says otherwise.

### 3.5 Testing the installed build

Release candidates also come as an installer, `EDLS-Setup-<version>.exe` (made by
`build.ps1`). It installs for your Windows user only, with no admin prompt, into
`%LOCALAPPDATA%\Programs\EDLS`, and adds **EDLS - Elite Dangerous Launch Suite** to the
Start menu. It uses the same `%LOCALAPPDATA%\EDLS` folder (settings and log) as a source
run, so back that folder up first (§2, rule 5).

What changes when you test the installed build:

- **The program is `EDLS.exe`.** Wherever a test says `python.exe` or "EDLS's python
  process" (ST-16, PR-04, §8.14, §8.16), look for `EDLS.exe` instead. "Start EDLS again"
  (SM-12, ST-15) means starting it again from the Start menu.
- **There's no terminal.** Errors (tracebacks) go to `launcher.log` as lines starting
  `CRITICAL Uncaught exception`. Attach `launcher.log` to every report.
- **Source-only tests don't apply:** SM-01, ST-07, ST-08, ST-09, ST-11, ST-13, ST-17,
  JB-01, JB-03 and JB-04.

| ID | Pri | Env | Steps | Expected |
| --- | --- | --- | --- | --- |
| IN-01 | P1 | A | Run the installer, with "Launch EDLS" ticked at the end | No UAC (admin) prompt. EDLS opens when setup finishes, not elevated (Task Manager → Details → add the "Elevated" column: it says No). The Start menu has "EDLS - Elite Dangerous Launch Suite". |
| IN-02 | P1 | A | With EDLS open, run the same or a newer installer | Setup offers to close EDLS and does. Steam, the game and the tools stay open. Afterwards your settings and log are unchanged. |
| IN-03 | P1 | A | Windows Settings → Apps → uninstall "Elite: Dangerous Launch Suite" | EDLS, its Start menu entry and its install folder are gone. `%LOCALAPPDATA%\EDLS` (settings and log) is kept. |
| IN-04 | P2 | A | Download the installer from the release page and run it | Record what SmartScreen and your antivirus say (the installer isn't code-signed). Its SHA256 (`Get-FileHash EDLS-Setup-<version>.exe`) matches the one in the release notes. |
| IN-05 | P3 | A | Install with "Create a desktop shortcut" ticked → start EDLS from it → right-click `EDLS.exe` in the install folder → Properties → Details | The shortcut works. Details shows the EDLS version, description and copyright. |

---

## 4. Test harness

**Why:** with a fake game and dummy apps you can test launching, monitoring and teardown
in seconds, as often as you like. You never start the real game and never risk real tool
data. EDLS identifies programs **by file name**, so a dummy named
`EliteDangerous64.exe` looks like the game to it.

**Step 1: build the dummy program.** Run this in **Windows PowerShell** (`powershell.exe`,
the classic blue one). It does not work in PowerShell 7. If you built it before version
1.2.1, build it again: it now has a "stubborn" mode.

```powershell
New-Item -ItemType Directory -Force C:\EDLS-QA | Out-Null
$src = @"
using System;
using System.IO;
using System.Windows.Forms;
public static class Dummy {
    [STAThread]
    public static void Main() {
        string name = Path.GetFileNameWithoutExtension(Application.ExecutablePath);
        Form f = new Form();
        f.Text = "QA dummy: " + name;
        f.Width = 380; f.Height = 120;
        Label l = new Label();
        l.Text = "Close this window to make '" + name + "' exit.";
        l.AutoSize = true; l.Left = 12; l.Top = 20;
        if (name.Contains("Stubborn")) {
            // Plays a tool that ignores being asked to close (one that hides in
            // the tray, say): only a forced close ends it.
            l.Text = "'" + name + "' won't close. End it in Task Manager > Details.";
            f.FormClosing += (s, e) => {
                if (e.CloseReason != CloseReason.WindowsShutDown) e.Cancel = true;
            };
        }
        f.Controls.Add(l);
        Application.Run(f);
    }
}
"@
Add-Type -TypeDefinition $src -ReferencedAssemblies System.Windows.Forms `
    -OutputAssembly C:\EDLS-QA\DummyTool.exe -OutputType WindowsApplication
```

**Step 2: make copies.** Each copy acts as a different app.

```powershell
Copy-Item C:\EDLS-QA\DummyTool.exe C:\EDLS-QA\DummyA.exe
Copy-Item C:\EDLS-QA\DummyTool.exe C:\EDLS-QA\DummyB.exe
Copy-Item C:\EDLS-QA\DummyTool.exe C:\EDLS-QA\DummyC.exe
Copy-Item C:\EDLS-QA\DummyTool.exe "C:\EDLS-QA\Dummy With Spaces.exe"
Copy-Item C:\EDLS-QA\DummyTool.exe C:\EDLS-QA\EliteDangerous64.exe   # the fake game
Copy-Item C:\EDLS-QA\DummyTool.exe C:\EDLS-QA\DummyStubborn.exe      # won't close (SC-20)
```

Each dummy opens a small window titled "QA dummy: &lt;name&gt;". **Closing that window
makes the program exit.** The exception is `DummyStubborn`: it refuses to close, like a
tool that hides in the tray. End it from Task Manager → Details → End task. If antivirus quarantines the freshly built file, allow it, or
use any small portable program renamed the same way.

**Step 3: stop LAUNCH from starting the real game.** Close EDLS. In `settings.json`,
change `"elite_app_id": 359320` to `"elite_app_id": 999999999` and save. LAUNCH now
sends Steam a launch request for a game that doesn't exist. Steam may show an error, a
store page, or nothing; dismiss whatever appears. EDLS then waits for
`EliteDangerous64.exe` to appear. You supply it by double-clicking
`C:\EDLS-QA\EliteDangerous64.exe`, **within 60 s**, or the launch is cancelled.
**Closing that window = "the game exited".** Steam still has to be installed for the
harness to work.

**Step 4: add the dummy apps.** Settings → **+ ADD** → Name `Dummy A` → FILE Browse… →
`C:\EDLS-QA\DummyA.exe` → Save. Repeat for B and C, then **Save** the Settings window.
Untick your real tools while you use the harness if you don't want them starting too.
Add `Dummy Stubborn` (`C:\EDLS-QA\DummyStubborn.exe`) only when a test asks for it
(SC-20), and untick it afterwards, since it won't close by itself.

**Step 5: clean up when done.** Put `elite_app_id` back to `359320` (or restore your
backup), remove the dummy apps, and close any dummy windows.

**Limits:** the harness checks EDLS's own logic. It can't tell you how real Steam,
min-ed-launcher or real tools behave. Those tests are marked **R**.

---

## 5. Quick reference

### 5.1 The main window

1. **Header.** One panel with cut corners. "ELITE : DANGEROUS · LAUNCH SUITE" on top,
   and the `CMDR` tag with your commander name in capitals underneath. The ringed-planet
   title mark spans both rows on the left. A gold line runs along the top edge (orange
   in light mode) and stops at the cut corner. The panel is about 45% see-through. When a
   newer EDLS is out, a green "UPDATE x.y.z AVAILABLE" badge sits at the right end of the
   CMDR row (§8.20).
2. *(The CMDR row is now part of the header, item 1.)*
3. **SYSTEM STATUS.** One card per app, four per row. The order is Steam, Elite, then
   your companion apps in the order Settings lists them.
4. **ACTIVITY LOG.** Timestamped, colour-coded messages. They're also written to
   `launcher.log`. You can drag the gap between SYSTEM STATUS and the log to resize them.
5. **Bottom bar.** Credits, the version badge and a "report issue" link on the left. The
   buttons **SHUTDOWN**, **AUTO START**, **SETTINGS**, **LAUNCH** on the right. (There's
   no REFRESH any more: cards update by themselves.)

The window can't be made smaller than 900×640. With many apps, the cards scroll inside
SYSTEM STATUS instead of making the window taller.

### 5.2 Status cards

Each card has a dot (it pulses while the app is live), the app NAME, a state on the
right, and a button at the bottom. Cards keep themselves up to date: EDLS checks what's
running about every 2 s while something is happening, and every 5 s when idle.

- **Cards for apps you've unticked in Settings:** dimmed name, a `DISABLED` tag instead
  of a button, and no state text while the app is offline. EDLS never starts or closes
  those apps.
- **Greyed-out buttons still answer clicks:** clicking one writes the reason to the
  activity log instead of doing nothing.
- **Elite card:** reads **STARTING** (greyed) while the game is starting, and **QUIT
  IN-GAME** (greyed) while it runs. EDLS can start the game but never stops it. For 3 s
  after the game finishes starting or closing, START stays greyed, to swallow an
  accidental double-click.

| State | Colour | Dot | Button | Meaning |
| --- | --- | --- | --- | --- |
| Offline | grey | still | START | Not running. Also what a tool shows after SHUTDOWN or the end-of-session teardown closed it. |
| Not found | grey | still | START | Enabled, but its program isn't at the configured path. |
| Failed | red | still | START | EDLS tried to start it and couldn't. |
| Stopped | grey | still | START | Stopped from its own card. |
| Waiting... | orange | pulsing | START (Elite: greyed STARTING) | Steam or Elite: EDLS is waiting for it to appear. |
| Launching... | orange | pulsing | STOP | Being started. |
| Online | green | pulsing | STOP | Running (Steam and tools). |
| Running | green | pulsing | STOP (Elite: greyed QUIT IN-GAME) | The game is running. |
| Error | red | still | **ERROR** (red) | EDLS tried to close it and couldn't; it's still running. Clicking ERROR tries again. |

Card borders also tint green, orange or red to match the state.

### 5.3 Buttons

- **LAUNCH.** Runs the full sequence: Steam → game → enabled tools → watch the game →
  close the tools when the game exits → (if the setting is on) EDLS counts down 5 s and
  closes itself. While a session runs, the button reads "RUNNING..." and is disabled.
  During the close countdown it reads **STAY OPEN**: click it and EDLS stays open.
  If the game hasn't appeared after **60 s**, the launch is cancelled and LAUNCH comes
  back. LAUNCH is also greyed out while the Elite card is starting the game on its own.
- **SHUTDOWN.** Closes companion apps right now (which ones depends on the scope, §5.4),
  without asking. It never closes Steam or the game. If the game is running it tells
  you to quit from the in-game menu. Then, per the "Close EDLS after SHUTDOWN" setting,
  EDLS either counts down and closes, or keeps waiting for the game to exit.
- **AUTO START.** An on/off switch; it turns orange when on. When on, EDLS runs LAUNCH by
  itself **the next time it opens**. Turning it on doesn't launch anything now.
- **SETTINGS.** Opens the settings window. During a LAUNCH session the companion app
  list in there is **locked** (everything else can still be changed). It unlocks when
  the session ends (the game closes) or when EDLS restarts.

### 5.4 Shutdown scope and closing EDLS

The Settings toggle "Shutdown closes all enabled apps (not just those EDLS launched)"
sets the scope:

- **Off (default, "launched only").** SHUTDOWN and the end-of-session teardown close only
  apps that this EDLS window started or took over. A tool that was already running when
  you pressed LAUNCH (or its card's START) is taken over, so it's closed too. One you
  open yourself mid-session is left alone.
- **On ("all enabled").** They close every *enabled* companion app that's running,
  whoever started it.
- Unticked (disabled) apps, Steam and the game are never closed, in either mode.

Two more toggles decide whether **EDLS itself** closes afterwards. Tools are closed
either way:

| Toggle | Default | When on | When off |
| --- | --- | --- | --- |
| Close EDLS when the game exits | on | After the teardown: "EDLS will close in 5 seconds." → "Farewell, CMDR. o7" → the window closes. STAY OPEN (the LAUNCH key) cancels it. | "Session ended." EDLS stays open, LAUNCH comes back and the app list unlocks. |
| Close EDLS after SHUTDOWN | off | After SHUTDOWN's teardown: the same 5 s countdown (STAY OPEN cancels it), then EDLS closes. The game keeps running. | EDLS stays open. During a session it logs "Waiting for Elite: Dangerous to close..." and handles the game exit as usual. |

If the countdown ends while a dialog is open (Settings, a popover, the file picker),
EDLS doesn't close behind it. It logs "EDLS stayed open - a dialog was open." and
carries on as if you'd clicked STAY OPEN.

**Tools that need admin rights.** EDLS never runs as administrator. Some tools ask Windows
for admin rights themselves; EDHM-UI-V3 does on any admin account with UAC on. The toggle
"Ask for admin rights when a tool needs them" (default **on**) decides what happens:

| | On (default) | Off |
| --- | --- | --- |
| Starting such a tool (LAUNCH or its card) | Windows shows its UAC prompt. Yes: the tool starts as administrator. No: the card shows Failed. | No prompt. The card shows Failed and the log says the tool needs administrator rights. |
| Closing one EDLS can't close (teardown, SHUTDOWN, card STOP) | After the normal 3 s attempt, Windows asks once for all of them. Yes: an elevated copy of EDLS closes them the usual way (asked first, forced after 3 s). No: the cards show ERROR. | No prompt. The cards show ERROR (SC-16). |

The prompt for closing names **EDLS.exe** (unknown publisher, because the build isn't
signed). Steam and the game are never started or closed as administrator.

### 5.5 What a normal session logs

Colours are for the dark theme. In the light theme the log sits on a mid-grey pane, so
it uses pale tints: white for normal lines, pale gold for success, pale orange for
warnings, pink for errors.

| Message | Colour | When |
| --- | --- | --- |
| Launch sequence initiated. | gold | LAUNCH pressed |
| Preflight checks... | dim | |
| &lt;App&gt; not found - will be skipped. | orange | only for an enabled app whose program is missing |
| Steam offline - launching... | amber | only if Steam was closed |
| Steam online. | gold | |
| Opening Elite: Dangerous via Steam... | amber | |
| Waiting for EliteDangerous64.exe... | amber | until the game process appears |
| Elite: Dangerous online. (PID 12345) | gold | |
| Launching &lt;App&gt;... / &lt;App&gt; online. (PID …) | amber / gold | each enabled tool |
| &lt;App&gt; already running - skipping. | dim | tool was already open |
| Failed to launch &lt;App&gt;. | orange | tool couldn't start |
| &lt;App&gt; needs administrator rights - Windows will ask. | amber | the tool asks for admin rights (EDHM-UI): the UAC prompt follows |
| &lt;App&gt; not started - the administrator prompt was declined. | orange | you answered No to that prompt |
| &lt;App&gt; needs administrator rights - not started. Turn on … | orange | same tool, with "Ask for admin rights when a tool needs them" off |
| All systems nominal. | gold | |
| Monitoring Elite: Dangerous... | dim | while you play |
| Elite: Dangerous offline. | amber | game exited |
| Closing third-party tools... / Stopping &lt;App&gt;... | amber | |
| Third-party tools shut down. *or* No tools running. | gold / dim | everything closed / nothing to close |
| Windows will ask for admin rights to close &lt;App&gt;. | amber | a tool that wouldn't close, with the admin toggle on: one UAC prompt for all of them |
| The administrator prompt was declined. | orange | you answered No to that prompt |
| Couldn't close &lt;App&gt; - it may be running as administrator. | red | a tool that still wouldn't close (its card turns ERROR); with the admin toggle off it adds a pointer to the setting |
| Some tools could not be closed: &lt;App&gt;, &lt;App&gt;. | orange | replaces "Third-party tools shut down." when one failed |
| EDLS will close in 5 seconds. | amber | "Close EDLS when the game exits" on |
| Farewell, CMDR. o7 | gold | 4 s later; the window closes 1 s after that |
| EDLS will stay open. | amber | you clicked STAY OPEN during the countdown |
| EDLS stayed open - a dialog was open. | amber | the countdown ended while Settings or a popover was open |
| Session ended. | gold | instead of the countdown, when that toggle is off |

Other messages you'll meet:

| Message | Colour | Comes from |
| --- | --- | --- |
| Elite: Dangerous Launch Suite ready. | gold | startup |
| settings.json: '…' has an invalid value - using … | orange | startup, a bad value in settings.json |
| settings.json couldn't be read (…) - settings were reset to defaults. The old file was kept as settings.json.bad. | orange | startup, unreadable settings.json |
| Status check: X of Y running. | dim | startup, once the first check finishes |
| &lt;App&gt; is now running. / &lt;App&gt; stopped running. | dim | a program opened or closed outside EDLS |
| Auto-start enabled — launching... | dim | startup with AUTO START on |
| Auto-start skipped - finish first-time setup first. | dim | AUTO START on, but setup isn't finished |
| Couldn't save settings.json - changes will be lost when EDLS closes (see launcher.log). | red | the file couldn't be written |
| Steam not up after 20s - continuing anyway. | orange | LAUNCH, Steam slow to start |
| Steam does not appear to be installed. | red | LAUNCH preflight |
| Elite: Dangerous didn't start within 60s - launch cancelled. Press LAUNCH to try again. | red | LAUNCH, game never appeared |
| Launch sequence already running. | orange | Steam/Elite card pressed during LAUNCH |
| The game is already being started from its card - wait for it to finish. | orange | LAUNCH clicked while greyed |
| Quit Elite: Dangerous from the in-game menu - EDLS never closes the game. | orange | SHUTDOWN while the game runs |
| Waiting for Elite: Dangerous to close... | dim | after that, with "Close EDLS after SHUTDOWN" off |
| The companion app list is locked during a session - it unlocks when the game closes, or when EDLS restarts. | orange | first attempt to edit the app list mid-session |
| Starting &lt;App&gt;... / &lt;App&gt; online. (PID …) | amber / gold | a tool's card START |
| &lt;App&gt; is already running. | dim | card START on a tool that's already open |
| &lt;App&gt; not found - check its path in Settings. | orange | card START, program missing (card: Not found) |
| Couldn't start &lt;App&gt; (see launcher.log). | red | card START failed (card: Failed) |
| Stopping &lt;App&gt;... / &lt;App&gt; stopped. | amber / gold | a tool's card STOP |
| &lt;App&gt; wasn't running. | dim | card STOP on something already closed |
| EDLS never closes &lt;App&gt; - it's Steam or the game. | orange | a custom app that points at Steam or the game |
| Launching Steam... / Steam online. (PID …) | amber / gold | Steam card START |
| Steam did not start within 20s. | red | Steam card START |
| Steam is already running. | dim | Steam card START |
| Asking Steam to exit... / Steam closed. | amber / gold | Steam card STOP |
| Steam is still running - it won't exit while a game or download is active. | orange | Steam card STOP |
| EDLS won't ask Steam to exit while Elite: Dangerous is running - quit the game first. | orange | Steam card STOP while the game runs |
| Starting the game only - no companion apps, no session monitor. Use LAUNCH for the full sequence. | dim | Elite card START |
| Elite: Dangerous is already running. | dim | Elite card START |
| Elite: Dangerous is already running - attaching. | dim | LAUNCH while the game is already running (no second launch request) |
| Elite: Dangerous did not start within 60s. | red | Elite card START |
| The game is already starting. / EDLS doesn't close Elite: Dangerous - quit from the in-game menu. / Give the game a moment before starting it again. | orange | clicking the greyed Elite card button |

### 5.6 Behaviour that's by design

Don't report these as bugs, unless they bother you as a player. That feedback is welcome.

- Cards update within about 2 s while something is happening (a session, or just after
  a click) and within about 5 s when EDLS is idle.
- When the game exits after a LAUNCH, EDLS closes the tools and then (by default)
  **closes itself** after a 5 s countdown (STAY OPEN cancels that).
- Closing EDLS never closes Steam, the game or the tools.
- The Elite card's START is "game only". It starts no tools and doesn't watch the game.
- In "Status check: X of Y running", Y counts Steam + Elite + **every** app in
  Settings, whether it's enabled or not and whether its card is shown or not.
- Under "launched only", a tool you opened before pressing LAUNCH is still closed at
  the end: LAUNCH takes it over. That's also what lets a re-attach after reopening EDLS
  (JR-04) clean up.
- Only one EDLS runs at a time. Starting a second copy brings the first to the front (or
  flashes its taskbar button) and the second quits.
- The window opens centred and sized to fit your screen. Its size and position aren't
  remembered between runs.

---

## 6. Smoke test

About 30 minutes. Run it first on every new build, before anything else.

| ID | Pri | Env | Steps | Expected |
| --- | --- | --- | --- | --- |
| SM-01 | P1 | A | `python selftest.py` | Ends with "N passed, 0 failed". Four "Refusing to kill protected process" lines and one "EDLS won't ask Steam to exit…" line are normal: they come from the checks that EDLS refuses. |
| SM-02 | P1 | A | `python main.py` | Window opens. The panels fade in top to bottom. No traceback and no `libpng` lines in the terminal. |
| SM-03 | P1 | A | Open and close a companion app yourself while EDLS is idle. Compare with Task Manager → Details. | Its card follows within about 5 s. Every card matches reality. |
| SM-04 | P1 | A | SETTINGS → change the CMDR name → Save → reopen SETTINGS | The CMDR row updates straight away. The new name is in the field. |
| SM-05 | P1 | A | SETTINGS → Light mode on → Save. Then switch back to dark. | Both themes look complete. Nothing is left in the wrong colours. |
| SM-06 | P1 | H | LAUNCH → open the fake game → dummies start → close the fake game | Dummies close. "EDLS will close in 5 seconds." → "Farewell, CMDR. o7" → EDLS closes itself. |
| SM-07 | P1 | R | LAUNCH → play → exit to desktop from the in-game menu | Tools close. **Steam stays open.** The countdown runs and EDLS closes itself. |
| SM-08 | P1 | R | LAUNCH → in game → SHUTDOWN | Tools close. "Quit Elite: Dangerous from the in-game menu…" and "Waiting for Elite: Dangerous to close..." appear. Game, Steam and EDLS keep running. |
| SM-09 | P1 | R | LAUNCH → in game → close EDLS with X | Game, Steam and tools all keep running. |
| SM-10 | P1 | R | While the game runs, look at the Elite card and click its button. | Greyed "QUIT IN-GAME". The click only adds a log line. |
| SM-11 | P1 | A | Close and reopen EDLS. | Settings are kept. No setup window appears. |
| SM-12 | P1 | A | With EDLS open, run `python main.py` again. | No second window. The first one comes to the front (or its taskbar button flashes). |

---

## 7. Regression checks

These cover recent changes. Each one needs a person to look at it. RG-01 to RG-08 cover
the first round (layout, background, the ToS fix). RG-09 onwards cover the fixes for the
risks from [§12](#12-known-risk-areas).

| ID | Pri | Env | Steps | Expected |
| --- | --- | --- | --- | --- |
| RG-01 | P1 | A | Get down to 4 or fewer cards: untick apps, and turn off "Show cards for apps that aren't enabled". | Cards stay a sensible height (about 90–110 px) at the top of the panel, with empty space below. They're **not** stretched tall. |
| RG-02 | P1 | A | Show every card (all apps visible). | Cards are the same height as in RG-01. |
| RG-03 | P1 | A | Look at the header, dark and light. | Title and CMDR name share one panel, with the same cut corners and hairline border as SYSTEM STATUS and ACTIVITY LOG. There's no separate CMDR strip. |
| RG-04 | P1 | A | Look at the header, dark and light. | The background art shows faintly through the header panel (about 45% see-through). The title, CMDR name, mark and top line are fully solid. The top line stops at the cut corner. |
| RG-05 | P1 | A | Start EDLS and switch theme both ways. Watch the terminal. | No `libpng error` lines. Starfield/nebula background with corner brackets in dark; the light background art in light. |
| RG-06 | P1 | H | Open the fake game and wait a few seconds. | Elite card: Running, with a greyed "QUIT IN-GAME" that fits its button. EDLS has no way to stop the game. |
| RG-07 | P1 | R | Run JB-01. | Closing EDLS that was started from VS Code doesn't close Steam or the game. |
| RG-08 | P1 | H | Run PR-07 and PR-08. | EDLS refuses to close anything named like the game or Steam. |
| RG-09 | P1 | A | Live status: run §8.8 (RF). | No REFRESH button. Cards follow programs opening and closing by themselves, in and out of a session. |
| RG-10 | P1 | H | ERROR and failed closes: run SC-16 and CC-18. | The card shows Error with a red ERROR button, the log says why, and the teardown summary names the tool. |
| RG-11 | P1 | H | Close-EDLS toggles and the countdown: run LS-04, LS-28, LS-29 and SC-12. | Countdown, farewell and close happen only when the matching toggle is on. |
| RG-12 | P1 | H | App list locked during a session: run SE-51 and SC-08 to SC-10. | No edits possible mid-session; popup plus one log line. |
| RG-13 | P1 | H | Elite card and LAUNCH grey each other out: run CC-15, CC-19 and SQ-22. | Only one launch at a time; greyed clicks are explained in the log. |
| RG-14 | P1 | H | 60 s game wait: run LS-13 (or its harness version LS-30) and CC-11. | Cancelled after 60 s with a clear message. A late game still flips the card to Running. |
| RG-15 | P1 | A | Bad settings.json: run PS-02 to PS-09 and PS-12. | No crash. Every problem is reported in the activity log. |
| RG-16 | P1 | A | Card logging: run CC-01 to CC-04. | Every card START/STOP leaves a log line, including the failures. |
| RG-17 | P1 | A | Small screens, keyboard, startup lock, one copy: run UI-03, UI-04, UI-12, UI-24, AX-01, AX-02, ST-15. | Fits 1024×768. Tab focus visible. No resizing during the fade-in. Never two copies. |
| RG-18 | P1 | H/R | This round (1.2.1): PR-10 (also works with the fake game open), LS-14, SE-20, SE-29, SE-35, SC-20, SC-16, and §3.5 | Steam is never asked to exit while the game runs. LAUNCH attaches to a running game without a second launch request. Quoted paths work. Steam and Elite can't be used as app names. An emptied app list stays empty. Tools are asked to close before they're forced. The installer installs, upgrades and uninstalls cleanly. |
| RG-19 | P1 | H/A | This round (1.3.0): FR-01, FR-08, SE-10, SE-38, SE-46, LG-04, LS-35, SC-22, SQ-37 | Only installed tools start ticked, and an upgrade doesn't tick a new default you don't have. Names show as typed. A renamed SrvSurvey is still found after a restart. EDLS never closes behind an open dialog. The log keeps your place. STAY OPEN cancels the close countdown. |
| RG-20 | P1 | H/A | Late 1.3.0 additions: SE-04, SE-55, SE-51, UP-02, UP-03, CA-19 and check g of §8.15 for your tools | Settings is landscape and fits 1024×768. Dragging a row sets the card and launch order, and can't be done mid-session. The update shows as a green badge on the CMDR name's row, at the right. With "Start tools minimized" on, tools that allow it start minimized. |

---

## 8. Detailed tests

### 8.1 EDLS startup and exit (ST)

| ID | Pri | Env | Steps | Expected |
| --- | --- | --- | --- | --- |
| ST-01 | P1 | A | Delete the whole `%LOCALAPPDATA%\EDLS` folder → start EDLS | Window opens centred (1000×880, or smaller to fit your screen). The four sections fade in within half a second. Then "EDLS — First-Time Setup" opens by itself. The log shows "Elite: Dangerous Launch Suite ready." and, a few seconds later, "Status check: N of 15 running.". The folder now contains `settings.json` and `launcher.log`. |
| ST-02 | P1 | A | Normal start (setup already done) | No setup window. Nothing flickers, and no extra window or taskbar button flashes in the first second. (EDLS quietly pre-builds the Settings window off-screen.) |
| ST-03 | P1 | R | Open Steam, the game and some tools by hand → start EDLS | Within about 5 seconds the cards show Online/Running for what's open. The Elite card shows QUIT IN-GAME. |
| ST-04 | P1 | A | Idle → close with X | Closes straight away. The terminal returns to the prompt with no traceback. |
| ST-05 | P2 | A | Idle → Alt+F4 | Same as ST-04. |
| ST-06 | P2 | A | Close EDLS within half a second of it appearing | Clean exit. No Settings window pops up afterwards. No traceback. |
| ST-07 | P2 | A | `cd \` then `python C:\path\to\EDLS\main.py` | Fonts, background, banner and icons all load, even though you started it from another folder. |
| ST-08 | P3 | A | `pythonw main.py` (no console) | Works. No console windows pop up later when EDLS starts Steam, the game or tools. |
| ST-09 | P2 | A | `py main.py` (Windows Python launcher) | Works. |
| ST-10 | P2 | A | Run the terminal "as administrator" → `python main.py` → LAUNCH | Record. Apps EDLS starts will also run as administrator (normal Windows behaviour). Check they work normally and no UAC prompts appear. |
| ST-11 | P3 | A | Put the repo in a folder with spaces and accented letters (e.g. `C:\Tests\Jörg's Tools\EDLS`) → run | Works normally. |
| ST-12 | P3 | A | Use a Windows account whose user name has spaces or accented letters | `settings.json` and `launcher.log` are created and used normally. |
| ST-13 | P2 | A | New venv without `pip install` → `python main.py` | Fails with a missing-module error in the terminal. Record what a non-technical user would see. |
| ST-14 | P2 | A | Look at the title bar and taskbar | The EDLS icon shows (record if the taskbar shows the Python icon instead). The title is "Elite: Dangerous · Launch Suite". |
| ST-15 | P1 | A | Start EDLS twice: from two terminals, by double-starting quickly, and once with the first window minimised | Never two windows. The second copy quits within about a second, and the first comes to the front (Windows may only flash its taskbar button). `launcher.log` says "EDLS is already running - showed that window instead." (R-23 fix) |
| ST-18 | P3 | A | Two different Windows users (fast user switching) each start EDLS | Both run: the one-copy rule is per user. |
| ST-16 | P2 | A | Idle → Task Manager → end EDLS's python process | Nothing else closes. The next start is normal. |
| ST-17 | P2 | A | Close the terminal window that's running EDLS | The EDLS window disappears. Steam, the game and tools are unaffected. |

### 8.2 First-time setup (FR)

| ID | Pri | Env | Steps | Expected |
| --- | --- | --- | --- | --- |
| FR-01 | P1 | A | Fresh start (ST-01). Inspect the setup window. | Title "EDLS — First-Time Setup". "WELCOME TO EDLS, CMDR" and an intro mentioning "report issue". CMDR NAME is empty, with the default name as grey ghost text and the caret in the field. Toggles: show cards **on**, shutdown-all **off**, ask for admin rights **on**, start tools minimized **off**, close EDLS when the game exits **on**, close EDLS after SHUTDOWN **off**, check for updates **on**, light mode **off**. The app list sits to the right of the toggles. All 13 default apps are listed, all visible without scrolling; only the ones installed on this PC are ticked (R-26 fix), and EDDI never is. Buttons: + ADD, EDIT, — REMOVE, RESTORE DEFAULTS, OPEN LOG FOLDER, CANCEL, BEGIN. |
| FR-02 | P1 | A | Enter a name → untick tools you don't have → Save → restart EDLS | The CMDR row shows the name in capitals. Unticked apps appear as dimmed DISABLED cards. After restart there's no setup window and everything is kept. |
| FR-03 | P1 | A | Fresh start → close the setup window with Cancel, then repeat with Esc, then with X | The window closes and the main window works with defaults. SETTINGS opens the first-run version again. On the next start the setup window reappears. |
| FR-04 | P2 | A | Turn AUTO START on → close EDLS → set `"setup_complete": false` in settings.json → start EDLS | The setup window opens and **no launch starts**. The log says "Auto-start skipped - finish first-time setup first." and never "Auto-start enabled — launching..." (R-34 fix). |
| FR-05 | P2 | A | In the setup window, type a name, clear it again → BEGIN | The default name is used. A blank name is never saved. |
| FR-06 | P2 | A | In the setup window, turn Light mode on → Save | The whole window switches to light. |
| FR-07 | P3 | R | Fresh start on a PC with only a few of the tools installed → BEGIN without changing the ticks → LAUNCH | Only the installed tools were ticked, so there's no pile of "… not found - will be skipped." lines (R-26 fix). A tool installed somewhere other than its default path starts unticked: tick it and set its path. |
| FR-08 | P1 | A | **Upgrade:** start 1.3.0 on a 1.2.1 `settings.json` (or delete the Elite Studio and EDDI entries from `settings.json` first) | Elite Studio and EDDI join the end of the app list: Elite Studio ticked only if it's installed, EDDI unticked even if it is. Every other tick is as you left it. An app you'd already added yourself as "EDDI" isn't added a second time. A VoiceAttack still on the old default path now finds VoiceAttack 2 as well (CA-09). |

### 8.3 Layout and visuals (UI)

Do this section in **both** themes.

| ID | Pri | Env | Steps | Expected |
| --- | --- | --- | --- | --- |
| UI-01 | P1 | A | Look over the dark theme | Starfield/nebula background with corner brackets behind see-through panels. No clipped text. Headings and buttons use the squared Euro Caps font; log and state text use a monospaced font. |
| UI-02 | P1 | A | Look over the light theme | Light-grey background art and a silver header with orange accents. SYSTEM STATUS and ACTIVITY LOG are grey see-through panes with dark cards ("Full Daylight"). Pale bottom and card buttons, with an orange LAUNCH. Every piece of text is readable, especially the pane titles and log lines on the grey. |
| UI-03 | P1 | A | Drag the window as small as it goes | It stops at 900×640. Nothing overlaps. All four bottom buttons are fully visible. The cards scroll if they don't fit. |
| UI-04 | P1 | A | Set Windows to 1024×768 (the smallest screen we support), then 1366×768, then 1920×1080 at 150% scaling. Start EDLS each time. | It opens centred and fully on screen, bottom buttons included, with the taskbar visible (R-18 fix). |
| UI-05 | P2 | A | Maximize → restore → snap to half screen (Win+← / Win+→), on 1920 wide and on 1366 wide | Layout adapts. Record whether half-screen snap works (the window can't be narrower than 900 px). |
| UI-06 | P2 | A | 4K at 150–200% scaling | Text is crisp. The button icons (▶ ⚙ ⟳ and the power symbol) are crisp. The banner title isn't pixelated. |
| UI-07 | P2 | A | Ultrawide (e.g. 3440×1440), maximized | The banner stops growing taller (about 88 px). The background covers the window, cropped but not stretched. Cards are wide but readable. |
| UI-08 | P2 | A | Two monitors with different scaling: drag the window between them | It re-renders at the right size on each, with no blurry text and no broken layout. |
| UI-09 | P1 | A | Header at minimum width and maximized | The title and CMDR name are centred and never overlap the title mark, which spans both rows on the left. Gold top line (orange in light mode). |
| UI-10 | P2 | A | Paste a 60-character CMDR name into Settings → Save. Then put a 60-character `"cmdr_name"` straight into `settings.json` → start EDLS. | Settings stops taking characters at 36. From `settings.json`: the log says "settings.json: 'cmdr_name' is longer than 36 characters - shortened." and the CMDR row shows the first 36. With ordinary names the window doesn't get wider. |
| UI-11 | P1 | A | Card grid | Four per row. Order: Steam, Elite, then the enabled apps in Settings order, then the disabled ones in Settings order. All cards the same height. With few cards they sit at the top with empty space below. |
| UI-12 | P1 | H | Add 20+ dummy apps (copies with different names) | The window doesn't grow. The cards scroll inside SYSTEM STATUS with a thin scrollbar, and the background still shows behind them (no dark box). The mouse wheel scrolls the cards (R-18 fix). |
| UI-13 | P2 | H | Add an app with a 40-character name | The card name is cut short with "…". The state text stays readable. Settings shows the full name. |
| UI-14 | P2 | A | Find the OdysseyMaterials card | It reads "ODYSSEY MATS HELPER". Settings still lists it as "OdysseyMaterials". |
| UI-15 | P1 | A | Show-cards toggle **on** → untick an app → Save | Its card stays: dimmed name, DISABLED tag, no button, same height as the others. No state text while it's offline; it still shows its real state by itself (Online if it's open). |
| UI-16 | P1 | A | Show-cards toggle **off** → Save | Unticked apps have no card at all. |
| UI-17 | P1 | H | Produce every state and check it against the [§5.2](#52-status-cards) table: Offline (idle), Not found (wrong path), Failed (path to a `.txt` file → START), Launching (briefly on START), Online, Stopped (STOP), Offline after SHUTDOWN, Waiting... (Elite during LAUNCH), Running (fake game), Error (see SC-16) | Text, colour, pulsing dot, button label and border tint all match the table. There is no "Closed" state any more. |
| UI-18 | P2 | H | Elite card with the fake game running, and during LAUNCH before the fake game opens | "QUIT IN-GAME" and "STARTING" fit inside the small button (not clipped) in both themes. Both are greyed out; clicking only writes a log line. |
| UI-19 | P2 | A | Drag the gap between SYSTEM STATUS and ACTIVITY LOG up and down, then resize the window | The log can't shrink below about 130 px. Nothing overlaps or disappears. |
| UI-20 | P2 | A | Hover and press each bottom button. Start a session to see LAUNCH disabled. | Hover brightens the border and text. Pressing darkens it. LAUNCH greys out and reads "RUNNING...". In light mode the buttons are pale and LAUNCH is solid orange; disabled LAUNCH is pale grey. |
| UI-21 | P2 | A | Toggle AUTO START | Off: grey like SETTINGS. On: orange border and text. The state survives a restart. |
| UI-22 | P2 | A | Click "report issue" | Your browser opens `https://github.com/coyoteware/EDLS/issues`. |
| UI-23 | P3 | A | Look at the version badge | Shows the version you were told you're testing (1.3.0 at the time of writing). |
| UI-24 | P2 | A | Start EDLS and try to maximize or resize it immediately (double-click the title bar or drag an edge during the half-second fade-in), then again right after | During the fade-in the window doesn't resize. Right after, it resizes normally and every panel sits in the right place (R-21 fix). |
| UI-25 | P3 | A | Switch Windows between dark and light mode | Record: the window title bar follows Windows, while the content follows EDLS's theme. Note any ugly clash. |
| UI-26 | P3 | A | Windows high-contrast mode | Record whether EDLS is still usable. |
| UI-27 | P3 | A | Windows Settings → Accessibility → Text size 150% | Record any clipped text. |

### 8.4 Launch session (LS)

| ID | Pri | Env | Steps | Expected |
| --- | --- | --- | --- | --- |
| LS-01 | P1 | R | Steam open, tools installed → LAUNCH | Log matches [§5.5](#55-what-a-normal-session-logs). Elite card goes Waiting... (orange) then Running. Each tool goes Launching... then Online and opens once. LAUNCH reads "RUNNING..." and is disabled. No black console windows flash. |
| LS-02 | P1 | R | Steam **closed** → LAUNCH | Steam card goes Launching... then Online within about 20 s. The game starts through min-ed-launcher. The rest is as LS-01. |
| LS-03 | P2 | R | Make Steam slow to start (cold boot, pending Steam update, or the Steam login screen) → LAUNCH | After 20 s the orange warning "Steam not up after 20s - continuing anyway." appears and the Steam card shows Waiting.... Once the game appears, the Steam card corrects itself to Online. |
| LS-04 | P1 | R | Mid-session → in-game menu → **Exit to desktop** ("Close EDLS when the game exits" on, the default) | Within about 2 s: "Elite: Dangerous offline." → "Closing third-party tools..." → "Stopping …" for each tool → "Third-party tools shut down." → "EDLS will close in 5 seconds." → about 4 s later "Farewell, CMDR. o7" → the window closes about 1 s after that. During the countdown LAUNCH reads STAY OPEN (LS-35). Tools close according to scope (§8.5). **Steam stays open.** No traceback. |
| LS-05 | P1 | R | Mid-session → exit to the **main menu** only | Nothing happens. Tools stay open and EDLS keeps watching. |
| LS-06 | P1 | H | LS-04 with the harness: close the fake game | Same sequence as LS-04. |
| LS-07 | P1 | A | Give one enabled tool a wrong path → LAUNCH | "… not found - will be skipped." The card shows Not found. Everything else launches. |
| LS-08 | P1 | A | Open a tool yourself → LAUNCH | "… already running - skipping." The card shows Online. No second copy opens. |
| LS-09 | P2 | A | Mid-session, close one tool yourself | Its card turns Offline within about 2 s. Nothing else happens and the session continues. |
| LS-10 | P2 | H | Point a dummy app at a `.txt` file → LAUNCH | "Failed to launch …" (orange). The card shows Failed (red). The rest of the sequence continues. |
| LS-11 | P1 | H | Right-click `DummyC.exe` → Properties → Compatibility → tick "Run this program as an administrator" → tick Dummy C in Settings → LAUNCH (EDLS not elevated, admin toggle on) → answer **Yes** to the UAC prompt | "Dummy C needs administrator rights - Windows will ask." → UAC prompt → "Dummy C online. (PID …)". The card shows Online, and Task Manager → Details shows Dummy C Elevated: Yes and EDLS Elevated: No. The tools after Dummy C in the list wait until you answer. Keep the compatibility box ticked for LS-32, LS-33, SC-21 and CC-20, and untick it afterwards. |
| LS-32 | P1 | H | LS-11, but answer **No** to the UAC prompt | Orange "Dummy C not started - the administrator prompt was declined." The card shows Failed, and the rest of the sequence carries on. |
| LS-33 | P1 | H | LS-11 with "Ask for admin rights when a tool needs them" **off** | No UAC prompt. Orange "Dummy C needs administrator rights - not started. Turn on 'Ask for admin rights' in Settings, or start it yourself." The card shows Failed. |
| LS-34 | P1 | R | Real EDHM-UI-V3 on an admin account, game in fullscreen → LAUNCH | The UAC prompt appears just after the game process starts. Record whether it takes the game out of fullscreen or focus, and whether EDHM-UI works normally as administrator. |
| LS-35 | P1 | H | Default settings → LAUNCH → close the fake game → during "EDLS will close in 5 seconds." click **STAY OPEN** (the LAUNCH key). Repeat, clicking it just after "Farewell, CMDR. o7". | "EDLS will stay open." EDLS stays open, the key goes back to LAUNCH and the app list unlocks. LAUNCH then starts a new session normally. Check the STAY OPEN key in both themes (R-36 fix). |
| LS-36 | P2 | R | Odyssey Materials Helper ticked and first in the app list, other tools after it → LAUNCH → as soon as its card shows Launching..., press SHUTDOWN | "SHUTDOWN pressed - no more tools will be started this session." SHUTDOWN waits for the helper to come up, then closes it. None of the tools after it start. The game keeps running and the session goes on as after any SHUTDOWN (R-44 fix). |
| LS-12 | P1 | A | Double-click LAUNCH fast | Only one sequence runs: one set of log lines and one game launch request. |
| LS-13 | P1 | R | Make the game launch fail (e.g. cancel the min-ed-launcher or Steam login, or go offline so authentication fails) → LAUNCH | After 60 s: red "Elite: Dangerous didn't start within 60s - launch cancelled. Press LAUNCH to try again.". The Elite card shows Failed, no tools start, and LAUNCH comes back (R-10 fix). Fix the cause → LAUNCH works. |
| LS-31 | P1 | R | **Cold start:** reboot, Steam closed → LAUNCH | The game appears within the 60 s. Record how long it took. If it was cancelled on a normal cold start, report it: the limit is too short for your PC (R-35). |
| LS-14 | P2 | R | Start the game from Steam directly (not EDLS) → in EDLS press LAUNCH | EDLS sends Steam no second launch request: the log says "Elite: Dangerous is already running - attaching.", and no error popup, second launcher or second game window appears (R-02 fix). It picks up the running game straight away, starts the tools, watches the game, and tears down when it exits. |
| LS-15 | P2 | R | LAUNCH → in game → close EDLS (X) → reopen EDLS | The game and tools keep running. The reopened EDLS shows them Online/Running. Then press SHUTDOWN: with "launched only" you get "No tools running." (this new window launched nothing, by design); with "all enabled" the enabled tools close. |
| LS-16 | P2 | R | Continue from LS-15: press LAUNCH to re-attach → play → exit the game | Teardown runs and EDLS closes. Note which tools closed (with "launched only", tools that were already open are closed too: LAUNCH took them over). |
| LS-17 | P2 | R | Play a session of 2+ hours → exit | Teardown still works at the end. Note EDLS memory (§8.16). |
| LS-18 | P2 | R | Mid-session → put the PC to sleep (or hibernate) → wake → keep playing → exit | EDLS is still watching, and teardown happens at exit. |
| LS-19 | P3 | R | Mid-session → unplug the network for a minute | No effect on EDLS. |
| LS-20 | P2 | R | Exit the game and relaunch it straight away (or use min-ed-launcher's auto-restart, if you use it for mission-board refreshes). Try it with "Close EDLS when the game exits" on, then off. | Record (R-30). The first exit ends the session and closes the tools. With the toggle on, EDLS closes too. With it off, EDLS stays: the Elite card shows the restarted game Running, and LAUNCH re-attaches and restarts the tools. Tell us whether that works for how you play. |
| LS-21 | P2 | R | One session each in Odyssey and Horizons/Legacy (if you own them) | The game is detected and torn down the same way in both. |
| LS-22 | P2 | R | Watch the game window while tools start | Record which tools pop up over the game or steal focus (e.g. knock a fullscreen game to the desktop). |
| LS-23 | P2 | H | Untick every app → LAUNCH → open the fake game → close it | "All systems nominal.", then at exit "No tools running." → countdown → Farewell → EDLS closes. |
| LS-24 | P2 | A | On a Windows account that has never run Steam (or a VM without Steam) → LAUNCH | The Steam card shows Not found. Red "Steam does not appear to be installed.". LAUNCH becomes clickable again. Nothing else starts. |
| LS-25 | P3 | A | Uninstall Steam but leave its registry entries (VM only) → LAUNCH | Record. Look for endless waiting or Windows "pick an app to open this link" prompts. |
| LS-26 | P2 | H | LAUNCH → open the fake game → close it the moment the tools appear | Teardown runs, the tools close, EDLS closes. No errors. |
| LS-27 | P2 | A | Enable all 13 defaults plus several dummies → LAUNCH | Everything starts. Note the total time and any app that fails to start when many start at once. |
| LS-28 | P1 | H | Settings → "Close EDLS when the game exits" **off** → LAUNCH → open the fake game → close it | Teardown runs, then "Session ended.". **No countdown**: EDLS stays open, LAUNCH comes back, and the app list in Settings is editable again. LAUNCH again works. |
| LS-29 | P2 | H | Default settings → LAUNCH → close the fake game → during "EDLS will close in 5 seconds." close the window yourself | It closes cleanly with no traceback, and nothing else happens afterwards. Also check that during the countdown LAUNCH reads STAY OPEN and clicking SETTINGS still works. |
| LS-30 | P1 | H | LAUNCH and don't open the fake game for 60 s → then open it | At 60 s the launch is cancelled exactly as in LS-13 and LAUNCH comes back. When you open the fake game, the Elite card flips to Running within about 5 s, with no tools started. LAUNCH then re-attaches. |

### 8.5 Shutdown scope (SC)

**Setup:** harness. Dummy A and Dummy B ticked, Dummy C unticked. Run each situation
twice: once ending with **SHUTDOWN**, once ending with **closing the fake game**. Both
should close the same apps. The difference is that after SHUTDOWN the game and EDLS keep
running.

| ID | Pri | Situation | "Launched only" (toggle off, default) | "All enabled" (toggle on) |
| --- | --- | --- | --- | --- |
| SC-01 | P1 | App started by LAUNCH | Closes | Closes |
| SC-02 | P1 | App started from its card's START button | Closes | Closes |
| SC-03 | P1 | App **you** opened before pressing LAUNCH (log: "already running - skipping") | Closes: LAUNCH took it over | Closes |
| SC-04 | P1 | App you opened yourself during the session | Stays open | Closes |
| SC-05 | P1 | App you opened yourself, no LAUNCH at all → SHUTDOWN | Stays open. Log: "No tools running." | Closes |
| SC-06 | P1 | Unticked (disabled) app that's running | Stays open | Stays open |
| SC-07 | P2 | App started by an earlier EDLS window (close EDLS, reopen it) | Stays open | Closes |
| SC-08 | P1 | Mid-session, try to **remove** an app in Settings | Not possible: popup "The companion app list is locked while a session is running…" and one activity-log line (R-15 fix). It closes as usual at the end. | Same |
| SC-09 | P1 | Mid-session, try to **rename** an app (EDIT or double-click) | Not possible: same popup (R-15 fix) | Same |
| SC-10 | P1 | Mid-session, try to **untick** an app | Not possible: the ON boxes are greyed out (R-32 fix) | Same |
| SC-11 | P2 | Flip the scope toggle in Settings mid-session (still allowed) | The new setting is used at teardown | The new setting is used at teardown |

More scope tests:

| ID | Pri | Env | Steps | Expected |
| --- | --- | --- | --- | --- |
| SC-12 | P1 | H | "Close EDLS after SHUTDOWN" off (default). Mid-session → SHUTDOWN → later close the fake game | SHUTDOWN closes the tools; their cards go straight to Offline (R-29 fix). Then "Quit Elite: Dangerous from the in-game menu - EDLS never closes the game." and "Waiting for Elite: Dangerous to close...". The game and EDLS keep running. At game exit: "No tools running." → countdown → Farewell → EDLS closes. |
| SC-18 | P1 | H | "Close EDLS after SHUTDOWN" **on**. Mid-session → SHUTDOWN | Tools close → the "quit in-game" line → "EDLS will close in 5 seconds." → Farewell → EDLS closes. **The fake game keeps running.** Reopen EDLS: the Elite card shows Running. |
| SC-19 | P2 | A | "Close EDLS after SHUTDOWN" on, idle with no game → SHUTDOWN | Tools close (or "No tools running."), no "quit in-game" line, then the countdown and EDLS closes. |
| SC-20 | P1 | H | Tick Dummy A and Dummy Stubborn. Idle: Dummy Stubborn's START, then its STOP. Then LAUNCH → open the fake game → close the fake game. | STOP: "Stopping Dummy Stubborn..." → about 3 s → "Dummy Stubborn stopped.". Teardown: Dummy A closes at once (it accepted being asked); Dummy Stubborn stays up about 3 s, then is force-closed. Both cards go Offline and the log says "Third-party tools shut down.". `launcher.log` has "Dummy Stubborn didn't close when asked - killing it." each time. The teardown takes at most about 3 s longer than before, however many tools refuse (R-17 change). |
| SC-13 | P2 | H | Double-click SHUTDOWN | One round of "Stopping …" lines. The second click logs a dim "Already closing the tools." (R-42 fix). |
| SC-14 | P2 | A | Idle with nothing running → SHUTDOWN | "No tools running." |
| SC-15 | P2 | H | LAUNCH → while it's waiting for the game, press SHUTDOWN → then open the fake game | SHUTDOWN closes per scope ("launched only" usually has nothing to close yet). LAUNCH keeps waiting. Once the fake game opens, tools start normally. |
| SC-21 | P1 | H | Admin toggle **on**. Start Dummy C as administrator (LS-11) and run Dummy A as administrator too → press Dummy C's STOP → answer Yes. Then start Dummy C again → switch scope to "all enabled" → SHUTDOWN → answer Yes. Repeat SHUTDOWN, answering No. | STOP: "Stopping Dummy C..." → about 3 s → "Windows will ask for admin rights to close Dummy C." → prompt (EDLS.exe) → "Dummy C stopped." SHUTDOWN: the other tools close first, then **one** prompt naming both dummies; both cards go Offline and "Third-party tools shut down." Answering No: "The administrator prompt was declined.", both cards show ERROR, and "Some tools could not be closed: …". No console window flashes. |
| SC-22 | P2 | H | "Close EDLS after SHUTDOWN" **on**. Mid-session → SHUTDOWN → click STAY OPEN during the countdown → later close the fake game | "EDLS will stay open.", then "Waiting for Elite: Dangerous to close...". The key goes back to "RUNNING..." (the session is still on). At game exit the session ends as usual: teardown, then the countdown if "Close EDLS when the game exits" is on. |
| SC-16 | P1 | H | Admin toggle **off**. EDLS not elevated. Right-click `DummyA.exe` → Run as administrator → wait for its card to show Online → press its card STOP. Then switch scope to "all enabled" → SHUTDOWN. | EDLS can't close it. After STOP (about 3 s: EDLS first waits for it to close by itself): red "Couldn't close Dummy A - it may be running as administrator. Turn on 'Ask for admin rights' in Settings to let EDLS close it.", and the card shows **Error** with a red **ERROR** button. After SHUTDOWN, the summary says "Some tools could not be closed: Dummy A." instead of "Third-party tools shut down." (R-11 fix). Close the dummy yourself: the card goes to Offline within about 5 s. |
| SC-17 | P2 | R | Tools that run several processes (see §8.15) → teardown | Task Manager shows no leftover processes from those tools, and their cards stay Offline. EDLS asks the main process to close and lets its same-name helpers follow (R-41). |
| SC-23 | P1 | H | Dummy Stubborn running (started by EDLS) → SHUTDOWN → close EDLS's window within a second. Then start EDLS again straight away. | The window disappears at once, and about 3 s later Dummy Stubborn is force-closed anyway. `launcher.log` ends with "Dummy Stubborn didn't close when asked - killing it." and "Third-party tools shut down.". The new EDLS opens a window normally (R-43 fix). Repeat with the card's STOP instead of SHUTDOWN: the same, with `launcher.log` ending "Dummy Stubborn stopped." |
| SC-24 | P2 | H | Admin toggle **on**. Dummy C running as administrator (LS-11) → SHUTDOWN → close EDLS's window within a second | No UAC prompt appears after the window has gone. Dummy C keeps running, and `launcher.log` says EDLS couldn't close it (R-43 fix). |

### 8.6 Steam and the game are never closed (PR)

Every test here is P1. **Any failure is a Blocker.**

| ID | Pri | Env | Steps | Expected |
| --- | --- | --- | --- | --- |
| PR-01 | P1 | R | In game, scope "launched only" → SHUTDOWN | Steam and the game are untouched. |
| PR-02 | P1 | R | In game, scope "all enabled" → SHUTDOWN | Steam and the game are untouched. |
| PR-03 | P1 | R | Exit the game normally (LS-04) | After teardown, Steam is still running. |
| PR-04 | P1 | R | In game, close EDLS each of these ways in turn: X; Alt+F4; Task Manager → end EDLS's python; close the terminal running EDLS | Steam, the game and tools are untouched every time. |
| PR-05 | P1 | R | **Steam closed** → LAUNCH (EDLS starts Steam) → in game → close EDLS | Steam and the game keep running. This exact case used to close the game, so test it carefully. |
| PR-06 | P1 | A | Game (or fake game) running → Elite card | Greyed "QUIT IN-GAME". Clicking it, or Tab to it and press Space, only logs "EDLS doesn't close Elite: Dangerous - quit from the in-game menu.". |
| PR-07 | P1 | H | Add a custom app "Fake Game" whose file is `C:\EDLS-QA\EliteDangerous64.exe` → scope "all enabled" → open the fake game → SHUTDOWN | The fake game is **not** closed. The activity log says "EDLS never closes Fake Game - it's Steam or the game." and `launcher.log` has "Refusing to kill protected process EliteDangerous64". |
| PR-08 | P1 | R | Add a custom app pointing at `C:\Program Files (x86)\Steam\steam.exe` → scope "all enabled" → SHUTDOWN | Steam is **not** closed. Same activity-log line; `launcher.log` has "Refusing to kill protected process steam". |
| PR-09 | P1 | H | Press STOP on the cards from PR-07 and PR-08 | Neither program closes. The "EDLS never closes …" line appears, and the cards keep showing Online. |
| PR-10 | P1 | R | **Docked, with nothing unsaved:** while the game runs, press STOP on the **Steam** card | EDLS refuses: orange "EDLS won't ask Steam to exit while Elite: Dangerous is running - quit the game first.". Steam is never asked, and Steam and the game keep running. If either closes, that's a Blocker (R-01 fix). |
| PR-11 | P2 | R | No game running → Steam card STOP. Repeat while a Steam download is active. | No download: Steam exits within about 15 s, "Steam closed.", card shows Stopped. With a download: "Steam is still running - it won't exit while a game or download is active." and Steam stays open. |

### 8.7 Card buttons (CC)

| ID | Pri | Env | Steps | Expected |
| --- | --- | --- | --- | --- |
| CC-01 | P1 | A | Idle → companion app START | Launching... then Online. The app opens once. The log shows "Starting X..." → "X online. (PID …)" (R-12 fix). |
| CC-02 | P1 | A | Companion app STOP | "Stopping X..." → "X stopped.". The app closes and the card shows Stopped. The window stays responsive while it closes. |
| CC-03 | P2 | A | START on an app whose path is wrong | The card shows **Not found** and the log says "X not found - check its path in Settings." (R-12 fix). Point it at a `.txt` file instead: the card shows **Failed** with "Couldn't start X (see launcher.log).". |
| CC-04 | P2 | A | Close an app yourself, then immediately (before its card catches up) press STOP | "X wasn't running." The card shows Stopped. Nothing else closes. |
| CC-05 | P2 | A | Open an app yourself, then immediately press START on its card | No second copy. "X is already running." The card shows Online. (EDLS takes it over: SHUTDOWN treats it as one it launched.) |
| CC-06 | P2 | A | Click START/STOP on one card 10 times fast | No crash and no extra copies. A click while the app is still starting or closing gets a dim "X is busy - wait for it to finish." and does nothing (R-42 fix). Within a few seconds the card shows the true state. |
| CC-07 | P1 | R | Steam closed → Steam card START | "Launching Steam..." → "Steam online. (PID …)". The card shows Online and Steam opens. |
| CC-08 | P3 | R | Steam START when Steam takes more than 20 s (e.g. a big update) | "Steam did not start within 20s." (red), card shows Failed. When Steam does open, the card flips to Online by itself. |
| CC-09 | P1 | R | Game closed → Elite card START | Dim "Starting the game only …" line → Waiting... (button greyed "STARTING") → Running. **No tools start.** EDLS doesn't run a session. |
| CC-10 | P2 | R | After CC-09, exit the game | The Elite card goes to Offline within about 5 s (R-13 fix). No tools close. EDLS stays open. START is greyed for 3 s, then usable. |
| CC-11 | P2 | H | Elite card START → don't open the fake game for 60 s → then open it | At 60 s: "Elite: Dangerous did not start within 60s." (red), card shows Failed. Opening the fake game afterwards flips the card to Running within about 5 s (R-28 fix). |
| CC-12 | P2 | A | Open the game (or fake game) and press the Elite card's START before the card catches up | "Elite: Dangerous is already running." → Running. |
| CC-13 | P1 | A | During a LAUNCH session (before the game appears), press the Steam card button | Orange "Launch sequence already running.". Nothing else happens. (The Elite card is greyed STARTING at that point: see CC-19.) |
| CC-14 | P2 | R | Steam closed → Steam START → straight away Elite START | The second click is ignored silently, because only one Steam/Elite action runs at a time. Record whether you'd expect a message. |
| CC-15 | P1 | H | Elite card START → while it's waiting, look at LAUNCH and click it | LAUNCH is greyed. Clicking it logs "The game is already being started from its card - wait for it to finish." and nothing launches. One launch request only (R-09 fix). LAUNCH returns once the card is done. |
| CC-16 | P2 | H | Mid-session: STOP a dummy → START it again → later close it yourself → end the session | Stop and start work. After you close it yourself, its card goes to Offline within about 2 s (R-16 fix). At the end of the session nothing complains. |
| CC-17 | P3 | A | Look at an unticked app's card | It has no button, so it can't be started from the card. |
| CC-20 | P1 | H | Admin toggle on, Dummy C set to run as administrator (LS-11) → click its card START twice quickly → answer Yes | One UAC prompt, one copy. The window stays responsive while the prompt is up, and the card shows Launching... then Online. |
| CC-21 | P1 | H | Admin toggle on, Dummy C running as administrator (LS-11) → click its card STOP twice quickly → answer Yes | One "Stopping Dummy C...", a dim "Dummy C is busy - wait for it to finish." for the second click, and **one** UAC prompt. "Dummy C stopped." (R-42 fix). |
| CC-22 | P2 | H | Dummy Stubborn and Dummy A running, scope "all enabled" → Dummy Stubborn's STOP → straight away SHUTDOWN | SHUTDOWN waits for the card's STOP to finish (about 3 s), then closes Dummy A. Dummy Stubborn is stopped once: one "Stopping Dummy Stubborn..." line (R-42 fix). |
| CC-18 | P1 | H | Admin toggle off. Produce an ERROR card (SC-16) → click **ERROR** while the dummy still runs as admin → then close EDLS, start it again as administrator and press STOP on that card | ERROR click: "Stopping …" and the same "Couldn't close …" line; the card stays Error. As administrator, STOP closes it and the card shows Stopped. |
| CC-19 | P1 | H | LAUNCH (fake game not open yet) → click the Elite card's greyed STARTING. Open the fake game → click its greyed QUIT IN-GAME. Close the fake game → click START straight away, and again after 3 s. | STARTING: "The game is already starting.". QUIT IN-GAME: "EDLS doesn't close Elite: Dangerous - quit from the in-game menu.". Straight after closing: "Give the game a moment before starting it again." and nothing starts. After 3 s, START works (R-09 fix). |

### 8.8 Live status (RF)

There's no REFRESH button any more. EDLS checks what's running by itself: about every
2 s while something is happening (a session, a card action, or within 20 s of a click),
about every 5 s otherwise.

| ID | Pri | Env | Steps | Expected |
| --- | --- | --- | --- | --- |
| RF-01 | P1 | A | Idle → open and close tools yourself → watch the cards | Each card follows within about 5 s (R-13 fix). The log gets a dim "X is now running." / "X stopped running." line for each change. |
| RF-02 | P2 | A | Read the startup line "Status check: X of Y running." | X = how many of Steam, Elite and every Settings app were running. Y = 2 + the number of apps in Settings, including unticked or hidden ones. |
| RF-03 | P2 | A | Leave EDLS idle for 10 minutes with nothing changing | No new log lines and no flicker: the cards stay put. |
| RF-04 | P2 | H | LAUNCH, don't open the fake game yet, and open/close a dummy meanwhile | The Elite card stays "Waiting..." the whole time; the dummy's card follows reality. |
| RF-05 | P2 | H | Elite card START → while it waits, save Settings and switch theme | The Elite card keeps "Waiting..." through both (R-14 fix). Once the fake game opens it shows Running. |
| RF-06 | P2 | A | Tools that hand over to a second program (see CA-13) | Record whether their cards are right. |
| RF-07 | P3 | A | Another Windows user signed in (fast user switching) has the game or a tool open | Record (R-31): does this EDLS show their programs as running? |
| RF-08 | P1 | A | Just after starting EDLS, drag the window around while the first check runs (it can take a few seconds on a fresh boot) | The window stays responsive and the cards fill in within about 5 s. |
| RF-09 | P3 | H | Two custom apps pointing at the same exe → open it | Record: both cards show Online for one program. Is that confusing? |
| RF-10 | P2 | H | Settings → add a dummy that's already running → Save | Its new card shows Online within a second or two. |
| RF-11 | P2 | A | Sleep the PC with EDLS idle → wake → open or close a tool | Cards still follow changes after waking. |

### 8.9 Settings window (SE)

**General**

| ID | Pri | Env | Steps | Expected |
| --- | --- | --- | --- | --- |
| SE-01 | P1 | A | SETTINGS | Opens quickly (under half a second). Title "EDLS — Settings", heading "S E T T I N G S". It shows the current values. It's styled like the main window: mono text, square tick boxes, and chamfered buttons with an orange or gold SAVE. Only the Windows title bar looks standard. |
| SE-02 | P1 | A | Change several things → Cancel. Repeat with Esc and with X. | Nothing is saved. Check by reopening, and again after a restart. |
| SE-03 | P1 | A | Change several things → Save | Applied straight away, and still there after a restart. |
| SE-04 | P2 | A | Resize the Settings window | It's landscape: CMDR name and toggles on the left, the app list on the right. It can't go below about 940×570 (940×640 for first-time setup). The app list grows with the window, the toggles keep their width, and the buttons stay visible. Only "Shutdown closes all enabled apps…" wraps onto a second line. On a 1024×768 screen it opens fully on screen. |
| SE-05 | P2 | A | Press Enter while typing in CMDR NAME | Record. It probably saves and closes immediately; say whether that surprised you. |

**CMDR name**

| ID | Pri | Env | Steps | Expected |
| --- | --- | --- | --- | --- |
| SE-06 | P1 | A | Change the name → Save | The CMDR row shows the new name in capitals. |
| SE-07 | P2 | A | Blank, or only spaces → Save | The name resets to the default. Reopen Settings: the field is empty and the default shows as grey ghost text. |
| SE-54 | P1 | A | With the default name, open Settings and look at CMDR NAME. Type a letter, then delete it. | The field is empty with the default name in grey and the caret blinking at the start. Typing replaces the grey text; emptying the field brings it back. A custom name shows as normal text. |
| SE-08 | P2 | A | "  Jameson  " with spaces at the ends → Save | Saved as "Jameson". |
| SE-09 | P2 | A | Accented letters, Cyrillic, Chinese/Japanese, an emoji | Shown correctly in the CMDR row. Record any boxes or missing characters. |
| SE-10 | P3 | A | `<b>Bold</b>` and `A & B <C>` | Shown literally in the CMDR row (in capitals: `<B>BOLD</B>`), never as formatting. Same as an app name, on its card (R-19 fix). |
| SE-11 | P2 | A | A 60+ character name | See UI-10. |

**Toggles**

| ID | Pri | Env | Steps | Expected |
| --- | --- | --- | --- | --- |
| SE-12 | P1 | A | Click each of the eight toggles | The seven on/off settings are chamfered switches: the knob slides across, the OFF/ON label swaps, and the knob turns gold (orange in light mode). Light mode is a wider moon/sun switch: the orange knob slides over and its icon turns from moon to sun. Save keeps it. Cancel undoes it. A mouse click doesn't leave a focus ring behind. |
| SE-13 | P2 | A | Keyboard only: Tab through the window and change each toggle | Tab reaches every toggle and a dashed orange outline shows which one has focus. Space or Enter flips it; ← turns it off, → on (R-20 fix). |
| SE-14 | P1 | A | "Show cards for apps that aren't enabled" on and off → Save | Cards for unticked apps appear and disappear (UI-15, UI-16). |
| SE-52 | P1 | A | Look at the toggles "Close EDLS when the game exits" and "Close EDLS after SHUTDOWN" | Defaults: on and off. They survive Save, restart and RESTORE DEFAULTS (back to on/off). What they do: LS-04, LS-28, SC-12, SC-18. |
| SE-57 | P1 | A | Look at "Start tools minimized" and hover its "i" | Default off. It survives Save and restart; RESTORE DEFAULTS turns it off again. The hover help says some tools ignore it (launchers like Odyssey Materials Helper, splash screens, tools that reopen where you left them) and that running tools aren't touched. What it does: [§8.15](#815-companion-app-compatibility-ca) check g, CA-19. |

**Companion apps list**

| ID | Pri | Env | Steps | Expected |
| --- | --- | --- | --- | --- |
| SE-15 | P1 | A | Look at the list | "DRAG TO REORDER" in grey at the top right of the list. Columns: ON tick box, NAME, PATH (no process column). Default paths show placeholders like `%ProgramFiles(x86)%\…`. The paths of SrvSurvey, OdysseyMaterials, VoiceAttack and EDDI are blank because EDLS finds them by itself. |
| SE-55 | P1 | A | Drag a row (by its name or path) up the list, then down, then to above the first row and below the last. Start one more drag and drop it back where it was. Do it in both themes. | While you drag, an orange line shows where the row will land, and there's no line when it would land where it already is. The row moves there, keeps its tick and path, and stays selected. The list doesn't jump. Save: the cards and LAUNCH follow the new order (UI-11), and it's kept after a restart. Cancel: the old order. Record whether the drag feels obvious. |
| SE-56 | P2 | A | Select a row → Ctrl+Down, Ctrl+Up, including at the top and bottom of the list | The row moves one place and stays selected. At the ends nothing happens. Save keeps the order. |
| SE-16 | P1 | A | Untick an app → Save → LAUNCH | It stays unticked after a restart and LAUNCH doesn't start it. |
| SE-17 | P1 | H | + ADD → Browse… → pick `DummyA.exe` | The file picker shows `.exe` files (with an "All files" option). The path fills in. Save adds the row, ticked. After saving Settings the card appears and LAUNCH starts it. |
| SE-18 | P1 | H | + ADD → type the path by hand | Same as SE-17. |
| SE-19 | P2 | H | + ADD with a path using a placeholder, e.g. `%SystemDrive%\EDLS-QA\Dummy With Spaces.exe` | It's found and launches (placeholders expand at launch, and spaces in the path are fine). |
| SE-20 | P2 | H | + ADD a new name and paste a path in quotes, as Explorer's "Copy as path" gives you: `"C:\EDLS-QA\DummyB.exe"` | The quotes are dropped: the row shows the path without them, and the app is found and launches (R-27 fix). |
| SE-21 | P2 | A | A path that doesn't exist → Save | Saved without a warning. LAUNCH says "not found - will be skipped" and the card shows Not found. Record whether you'd want a warning when saving. |
| SE-22 | P2 | A | Path to a **folder** → LAUNCH | The card shows Failed. No crash. |
| SE-23 | P2 | A | Path to a **shortcut** (`.lnk`) → LAUNCH | Record (probably Failed). |
| SE-24 | P3 | A | Path to a `.bat` or `.cmd` → LAUNCH | Record: does a console window appear? Is the card status right afterwards? |
| SE-25 | P3 | A | Path on an unplugged USB drive or an unreachable network share → LAUNCH | Not found. No long freeze. |
| SE-26 | P2 | A | + ADD with a name but an empty path | Allowed. The card shows Not found on LAUNCH. |
| SE-27 | P1 | A | + ADD with a blank name | A red line under the fields: "Please enter a name for the app." The add window stays open, with no extra popup. |
| SE-28 | P1 | A | + ADD with an existing name in different capitals (e.g. `dummy a`) | A red line: "An app named '…' already exists. …". |
| SE-29 | P1 | A | + ADD with the name `Steam`, then `Elite` | Rejected like a duplicate, with the red line "An app named '…' already exists. …". Also in other capitals (`elite`). Names that merely contain them (e.g. `Elite Observatory 2`) are fine (R-08 fix). |
| SE-30 | P2 | A | Name with spaces at the ends | Trimmed. |
| SE-31 | P1 | A | Edit a row by double-clicking it, and again via select → EDIT | The edit window opens pre-filled. Changes are saved to the row. Keeping the same name gives no duplicate warning. |
| SE-32 | P2 | A | EDIT and — REMOVE with no row selected | Nothing happens and there's no error. |
| SE-33 | P1 | A | Remove a custom app → Save | Gone from the list and from the cards. |
| SE-34 | P1 | A | Remove a default app (e.g. EDCoPilot) → Save → restart EDLS | It stays removed and doesn't come back. |
| SE-35 | P2 | A | Remove **every** app → Save → restart EDLS | The list stays empty: only the Steam and Elite cards are left. RESTORE DEFAULTS → Save brings the 13 back, ticked only if installed (EDDI never) (R-06 fix). |
| SE-36 | P2 | A | After SE-34, add an app with the removed default's exact name and path | Works and launches. |
| SE-37 | P2 | A | Rename EDMarketConnector to "EDMC" → Save → restart → LAUNCH | Still launches. The original "EDMarketConnector" row doesn't reappear. |
| SE-38 | P2 | A | Rename SrvSurvey (path blank) → Save → LAUNCH → restart EDLS → LAUNCH | Found both times: the renamed app keeps its auto-find after the restart (R-07 fix). |
| SE-39 | P2 | A | Clear the path of a default app that has one (e.g. EDMarketConnector) → Save → reopen | The default path comes back. Record whether that's confusing. |
| SE-40 | P3 | A | EDIT a default app whose path has a `%…%` placeholder → Browse… | Record which folder the file picker opens in. |
| SE-41 | P1 | A | RESTORE DEFAULTS → **CANCEL**. Then RESTORE DEFAULTS → **RESET** → Cancel. Then RESTORE DEFAULTS → RESET → Save. | The confirmation is a chamfered popover over Settings, and Enter picks CANCEL. CANCEL: nothing changes. RESET: every field resets in the window (the name field empties to the grey default, default toggles, the 13 defaults, ticked only if installed, custom apps gone). Cancel: nothing saved. Save: the defaults are kept after a restart. AUTO START is unchanged. |
| SE-42 | P2 | A | Remove some defaults → Save → RESTORE DEFAULTS → Yes → Save → restart | All 13 are back and stay back. |
| SE-43 | P2 | A | OPEN LOG FOLDER | Explorer opens `%LOCALAPPDATA%\EDLS`. |
| SE-44 | P2 | H | 25+ apps in the list. Drag a row from the top to the bottom. | The list scrolls, and scrolls by itself while you hold a dragged row near its top or bottom edge. Save works. (Cards: UI-12.) |
| SE-45 | P2 | H | Open Settings mid-session and change things other than the app list (name, theme, toggles, scope) | Allowed. Changes take effect straight away for the rest of the session. See §9. |
| SE-51 | P1 | H | LAUNCH → open Settings → try every way of changing the app list: + ADD, EDIT, double-click a row, — REMOVE, RESTORE DEFAULTS, Space/Enter/Delete on a row, Ctrl+Up/Down, dragging a row, the ON boxes | The list header says "LOCKED WHILE A SESSION IS RUNNING" instead of "DRAG TO REORDER", and the ON boxes are greyed. A row can't be dragged. Every other attempt shows the popup "The companion app list is locked while a session is running…" and changes nothing. The activity log gets **one** warning per Settings opening, not one per attempt. After the game closes (with "Close EDLS when the game exits" off), reopen Settings: it's unlocked (R-15 fix). |
| SE-53 | P2 | A | In the app list, select a row with the arrow keys → Space → Enter → Delete | Space ticks or unticks ON; Enter opens the edit window; Delete removes the row (R-20 fix). |
| SE-46 | P2 | H | Leave Settings open while the fake game closes (session ends, countdown runs) | When the countdown ends, EDLS stays open behind Settings, logs "EDLS stayed open - a dialog was open." and LAUNCH comes back. Save and Cancel still work. No traceback (R-22 fix). |
| SE-47 | P2 | A | Settings, the add/edit popover (with an error showing), and the "Restore defaults" and "App list locked" popovers, in both themes | Everything is readable. Nothing is light-on-light or dark-on-dark. The popovers have no Windows title bar: chamfered panels with an orange or amber edge, centred over Settings. Esc closes them. |
| SE-48 | P3 | A | Very long paths in the list | Record whether you can still tell which app is which. |
| SE-49 | P2 | A | Add/edit window → change things → Cancel | The row is unchanged. |
| SE-50 | P2 | H | EDIT Dummy A's path → `DummyB.exe` → Save → open DummyB yourself | Within a few seconds Dummy A's card follows the new program and shows Online. |

### 8.10 settings.json robustness (PS)

Close EDLS before each edit. Keep a backup.

| ID | Pri | Env | Steps | Expected |
| --- | --- | --- | --- | --- |
| PS-01 | P1 | A | Delete `settings.json` → start | First-time setup (FR-01). |
| PS-02 | P1 | A | Replace the contents with `hello` → start. Repeat with `[1, 2]`. | No crash. Settings reset to defaults and the setup window appears. The activity log shows an orange "settings.json couldn't be read (…) - settings were reset to defaults. The old file was kept as settings.json.bad.", and `settings.json.bad` holds what you wrote. |
| PS-03 | P1 | A | Open `settings.json` in Notepad → Save As → Encoding: **UTF-8 with BOM** (change nothing else) → start | Settings kept exactly; no warnings (R-05 fix). |
| PS-04 | P1 | A | `"elite_app_id": "abc"` → start | No crash. The activity log shows "settings.json: 'elite_app_id' has an invalid value - using 359320."; everything else is kept (R-04 fix). |
| PS-05 | P2 | A | `"apps": null` → start | No crash. The 13 default apps are restored, ticked only if installed (EDDI never). |
| PS-06 | P2 | A | Make one entry in `apps` a plain string, e.g. `"EDMC"` → start | No crash. That entry is skipped with a warning ("skipped an app entry with no valid name"); the other apps load. |
| PS-07 | P2 | A | `"removed_defaults": null` → start | No crash. |
| PS-08 | P2 | A | `"cmdr_name": 123` (a number, no quotes) → start | No crash. Warning, and the default name is used. |
| PS-09 | P3 | A | An app with `"enabled": "false"` (in quotes). Also try `"no"`, `"0"`, `"maybe"`. | "false", "no", "0" untick the app. "maybe" gives a warning and keeps it ticked. |
| PS-10 | P3 | A | `"shutdown_scope": "nonsense"`, `"theme": "blue"` → start | No crash. Warnings for both; falls back to "launched only" and dark. |
| PS-11 | P3 | A | Add an unknown key, e.g. `"foo": 1` → start | Ignored. It disappears the next time EDLS saves. |
| PS-12 | P1 | A | Make `settings.json` read-only (Properties → Read-only) → start → change settings → Save → toggle AUTO START → restart | Each save shows a red "Couldn't save settings.json - changes will be lost when EDLS closes (see launcher.log)." After the restart the changes are gone, as warned (R-24 fix). No crash. |
| PS-18 | P2 | A | `"close_on_game_exit": false`, `"close_on_shutdown": "yes"` → start → open Settings | The two toggles show off and on. |
| PS-19 | P2 | A | `"allow_elevation": "no"` → start → open Settings. Then delete the line → start. | The first time, "Ask for admin rights when a tool needs them" shows off. Without the line it's on (the default), with no warning. |
| PS-13 | P2 | A | While EDLS runs, try to delete the EDLS folder | Windows may refuse because `launcher.log` is in use, which is fine. If it does get deleted, changing a setting recreates it with no crash. |
| PS-14 | P2 | A | Remove the `"version"` line → start | Loads fine. The file is rewritten with `"version": 1`. |
| PS-15 | P3 | A | Copy `settings.json` to another PC or Windows user → start there | Loads. `%…%` paths adapt to that machine. |
| PS-16 | P2 | A | Set every setting to a non-default value → restart | Everything comes back identical. |
| PS-17 | P2 | A | Toggle AUTO START and look at the file without opening Settings | `"auto_start"` changes in the file immediately. |

### 8.11 Theme (TH)

| ID | Pri | Env | Steps | Expected |
| --- | --- | --- | --- | --- |
| TH-01 | P1 | A | Dark → Light (Settings → Save) | Everything switches at once with no restart: background, banner, CMDR row, panels, cards, log, buttons. |
| TH-02 | P1 | A | Light → Dark | Same. |
| TH-03 | P1 | A | Fill the log with lines → switch theme | The same log lines are still there afterwards. |
| TH-04 | P1 | A | Restart in light mode | Opens straight into light, with no flash of dark first. |
| TH-05 | P2 | H | Switch theme mid-session, both while LAUNCH waits for the game and after it's running | LAUNCH stays "RUNNING..." and disabled. Every card keeps its state through the switch, including "Waiting..." and any Error (R-14 fix). Closing the fake game still triggers teardown. |
| TH-06 | P2 | A | Switch theme → open Settings and the add/edit window | They use the new theme. |
| TH-07 | P2 | A | Switch theme back and forth 10 times | No slowdown and nothing in the terminal. Memory stays roughly flat (Task Manager). |
| TH-08 | P3 | A | Light mode, bottom bar | Pale SHUTDOWN / AUTO START / SETTINGS and a solid orange LAUNCH with a white ▶. AUTO START turns pale orange when it's on. |
| TH-09 | P2 | A | Light mode, card START/STOP buttons | START is pale grey and STOP is pale orange, both on the dark cards. Hovering turns the border and text orange. Both are readable. |

### 8.12 Auto start (AS)

| ID | Pri | Env | Steps | Expected |
| --- | --- | --- | --- | --- |
| AS-01 | P1 | R | AUTO START on → restart EDLS | Nothing launches when you switch it on. On restart, once the panels appear, the log shows "Auto-start enabled — launching..." and LAUNCH runs by itself. |
| AS-02 | P1 | A | AUTO START off → restart | No automatic launch. |
| AS-03 | P2 | R | AUTO START on, Steam closed → start EDLS | Same as LS-02. |
| AS-04 | P2 | A | AUTO START on → start EDLS and close it within the first second | No launch happens: no Steam or game start, ever (the late auto-start can't fire after the window has closed). |
| AS-05 | P2 | A | AUTO START on → change and save Settings | AUTO START is still on. |
| AS-06 | P2 | A | AUTO START on → RESTORE DEFAULTS → Save | AUTO START is unchanged. Record whether that's what you'd expect. |
| AS-07 | P2 | A | AUTO START on with setup not finished | See FR-04. |

### 8.13 Activity log (LG)

| ID | Pri | Env | Steps | Expected |
| --- | --- | --- | --- | --- |
| LG-01 | P1 | A | Read a full session's log in both themes | Every line starts with `[HH:MM:SS]`. Success, normal, warning, error and dim lines are each recognisable. Check light mode especially (R-33 fix: pale gold for success, pale orange for warnings). |
| LG-02 | P1 | A | Compare with `launcher.log` | Same messages, plus full dates and levels. The file also has lines the panel doesn't, e.g. "=== Session started ===" and "&lt;App&gt; didn't close when asked - killing it.". |
| LG-03 | P2 | A | Select log text → Ctrl+C → paste somewhere | The copied text is correct. |
| LG-04 | P2 | A | Scroll up in the log while new lines arrive (open and close a dummy app repeatedly) | The view stays where you scrolled to. Scroll back to the bottom and it follows new lines again (R-25 fix). |
| LG-05 | P2 | H | Name a dummy app `A&B <test>` → LAUNCH | The log shows the name exactly, with nothing swallowed. |
| LG-06 | P3 | A | Grow `launcher.log` past 500 KB (long use, or opening and closing tools many times) | `launcher.log.1` appears and logging carries on. |
| LG-07 | P3 | A | Thousands of lines in the panel | Still responsive. |
| LG-08 | P2 | A | Look at the log header | "A C T I V I T Y   L O G" on the left, "launcher.log" on the right. |
| LG-09 | P2 | A | Read every message you see during testing | Report typos, unclear wording, or wrong colours. |

### 8.14 Closing EDLS safely (JB)

Background: if EDLS is started by something that cleans up after itself, such as VS
Code's debugger, Windows used to close everything EDLS had started when EDLS closed,
including Steam and the game. That's fixed. These tests guard it.

| ID | Pri | Env | Steps | Expected |
| --- | --- | --- | --- | --- |
| JB-01 | P1 | R | In VS Code: Run and Debug → "EDLS: Run app". **Steam closed.** LAUNCH → in game → stop debugging (Shift+F5) | Steam, the game and the tools all keep running. |
| JB-02 | P1 | H | JB-01 with the harness | Steam, the fake game and the dummies keep running. |
| JB-03 | P2 | R | During JB-01, in Process Explorer, double-click EDLS's `python.exe` (command line ends in `main.py`) → **Job** tab | `steam.exe`, `EliteDangerous64.exe` and your tools are **not** in the job's process list. |
| JB-04 | P2 | R | Repeat JB-01 starting EDLS from: `py main.py`; `.venv\Scripts\python.exe main.py`; `pythonw main.py`; Windows Terminal; plain `cmd.exe` | Closing EDLS never closes anything else. |
| JB-05 | P2 | R | Watch the screen while EDLS starts Steam or the game | No black console window flashes. |
| JB-06 | P2 | R | End EDLS from Task Manager mid-session | Steam, the game and tools keep running. |

### 8.15 Companion app compatibility (CA)

For every default app you have installed, run checks **a–g** and note the app's version.
Treat these as **P1** for EDMarketConnector and any tool you use daily, **P2** for the
rest.

- **a. Found:** at the default location, the card isn't "Not found". If it's installed
  somewhere else, fixing the path in Settings works.
- **b. Starts once:** LAUNCH opens exactly one copy: no duplicate windows or tray icons.
- **c. Status:** the card shows Online while it runs. Close it yourself: the card turns
  Offline within about 2 s in a session, about 5 s when idle.
- **d. Closes completely:** after teardown or STOP, Task Manager → Details shows **no**
  leftover processes from it (helpers, child processes, plugin processes). Also look for
  tray icons that only vanish when you hover over them.
- **e. Healthy after EDLS closes it:** EDLS asks the tool to close first and forces it
  only if it's still running 3 s later. When it had to force it, `launcher.log` says
  "&lt;App&gt; didn't close when asked - killing it.": note which one it was. Then start it
  again yourself. There's no "didn't shut down properly" prompt, no lost settings, and its
  data (logs, databases, profiles) is intact. **Any data loss is Critical** (R-17).
- **f. No surprises:** no extra console windows, updater prompts or errors when EDLS
  starts it.
- **g. Starts minimized:** with "Start tools minimized" on in Settings, LAUNCH starts it
  minimized on the taskbar and the game keeps focus. It's only a request to Windows, so
  record which tools open normally anyway. Expect Odyssey Materials Helper to (it starts
  through its launcher), and maybe tools with a splash screen or that reopen where you
  left them.

| ID | App (card name) | Where EDLS looks by default | Program name EDLS watches | Extra things to check |
| --- | --- | --- | --- | --- |
| CA-01 | EDMarketConnector | `%ProgramFiles(x86)%\EDMarketConnector\EDMarketConnector.exe` | EDMarketConnector.exe | Plugins that start their own helper programs (e.g. overlay plugins). Make sure those don't keep running after EDMC is closed. |
| CA-02 | SrvSurvey | Searched for under `%LOCALAPPDATA%\Apps\2.0` (ClickOnce install) | SrvSurvey.exe | Is it found? How long does LAUNCH take to get past it? Does starting it this way skip its auto-updates? |
| CA-03 | OdysseyMaterials ("ODYSSEY MATS HELPER") | Its launcher, `%LOCALAPPDATA%\Elite Dangerous Odyssey Materials Helper Launcher\Elite Dangerous Odyssey Materials Helper Launcher.exe`; if that's missing, `...\program\Elite Dangerous Odyssey Materials Helper.exe` | Elite Dangerous Odyssey Materials Helper.exe | The launcher checks for updates, starts the helper and exits. The card shows Launching until the helper appears (up to 30 s while it updates), then Online with the helper's PID (matches Task Manager). No "stopped running" / "is now running" lines in between. An install that had the old default path saved moves to the launcher by itself; a path you set yourself is kept. |
| CA-04 | EDCoPilot | `C:\EDCoPilot\EDCoPilot.exe` | EDCoPilot.exe | Uses more than one process? Check they all close. Check its own "close with the game" option doesn't conflict. |
| CA-05 | EDHM_UI | `%LOCALAPPDATA%\EDHM-UI-V3\EDHM-UI-V3.exe` | EDHM-UI-V3.exe | Its manifest asks for admin rights (`highestAvailable`), so on an admin account it gets a UAC prompt to start and another to close (§5.4, LS-34). On a standard account it should start with no prompt. It's Electron, so it runs several `EDHM-UI-V3.exe` processes (one main window process plus helpers): after teardown and after STOP, Task Manager → Details shows **no** `EDHM-UI-V3.exe` left, and the card stays Offline/Stopped (it doesn't flip back to Online a few seconds later). If `launcher.log` says "EDHM_UI didn't close when asked", check whether EDHM-UI hides to the tray when you close its window. Closing it mid-game doesn't mess up the in-game HUD colours. |
| CA-06 | opentrack | `%ProgramFiles(x86)%\opentrack\opentrack.exe` | opentrack.exe | After a forced close, the camera/tracker works again on the next start. |
| CA-07 | ICARUS Terminal | `%ProgramFiles(x86)%\ICARUS Terminal\ICARUS Terminal.exe` | ICARUS Terminal.exe | Probably runs several processes. Check they all close. |
| CA-08 | EDDiscovery | `%ProgramFiles%\EDDiscovery\EDDiscovery.exe` | EDDiscovery.exe | It has a local database. After a forced close it must open normally, with no repair or "database locked" messages and with recent data present. |
| CA-09 | VoiceAttack | The first one that's there: v2 `%ProgramFiles%\VoiceAttack2\VoiceAttack.exe`, v2 in Steam `%ProgramFiles(x86)%\Steam\steamapps\common\VoiceAttack2\VoiceAttack.exe`, v1 64-bit `%ProgramFiles%\VoiceAttack\VoiceAttack.exe`, v1 32-bit `%ProgramFiles(x86)%\VoiceAttack\VoiceAttack.exe`, v1 in Steam `...\steamapps\common\VoiceAttack\VoiceAttack.exe` | VoiceAttack.exe | Record which version you have (1.x or 2.x) and where it's installed. With both installed, v2 is the one that starts. A `settings.json` still holding the old default path (`%ProgramFiles%\VoiceAttack\VoiceAttack.exe`) moves to the search by itself; a path you set yourself is kept. Often run as administrator: see SC-16 and SC-21. Profiles and settings intact. |
| CA-10 | EDCoPTER | `%LOCALAPPDATA%\Programs\EDCoPTER\EDCoPTER.exe` | EDCoPTER.exe | General checks a–g. |
| CA-11 | Elite Observatory | `%LOCALAPPDATA%\Programs\Elite Observatory\ObservatoryCore.exe` | ObservatoryCore.exe | General checks a–g. |
| CA-18 | Elite Studio | `%ProgramFiles%\Elite Studio\EliteStudio.exe` | EliteStudio.exe | It uses WebView2, so it runs several `msedgewebview2.exe` processes under it. After teardown and after STOP, none of those are left (other apps such as Edge run their own, so count `msedgewebview2.exe` in Task Manager → Details before starting it and after closing it). If `launcher.log` says "Elite Studio didn't close when asked", check whether it hides to the tray when you close its window. |
| CA-20 | EDDI | `%LOCALAPPDATA%\EDDI\Application\EDDI.exe` (installed for you only, the installer's default); if that's missing, `%ProgramFiles%\EDDI\EDDI.exe` (installed for all users) | EDDI.exe | Settings shows its path blank: EDLS finds it by itself. It's added unticked even when installed: tick it to test. It shows a splash screen first, so check g may not hold. **With VoiceAttack 2** (EDDI's plugin starts EDDI itself, with its window hidden): 1) VoiceAttack ticked, EDDI unticked → LAUNCH → exit the game: EDDI starts with VoiceAttack and is gone after teardown. 2) Both ticked → LAUNCH a few times: record any "EDDI is already running" message, and whether VoiceAttack's EDDI commands work. 3) Both ticked, but start VoiceAttack yourself and wait for EDDI to appear before LAUNCH: EDDI's hidden window ignores a close request, so `launcher.log` may say "EDDI didn't close when asked - killing it.": do check e. |

More:

| ID | Pri | Env | Steps | Expected |
| --- | --- | --- | --- | --- |
| CA-12 | P2 | A | A tool installed somewhere non-default → EDIT its path → LAUNCH | Found, launched and closed normally. |
| CA-13 | P2 | A | Any tool whose program starts a *different* program and then exits ("launcher stub") | Record which tools do this. Odyssey Materials Helper's launcher is handled (CA-03). Any other stub added through Settings: its card will show Offline about 2 s after starting while the real program keeps running, and EDLS can't close the real program. |
| CA-14 | P2 | A | A tool that's already open but minimised to the tray → LAUNCH | Detected as running. No second copy. |
| CA-15 | P2 | A | A tool that shows an update prompt on start | Record whether the prompt blocks anything or confuses EDLS. |
| CA-16 | P2 | R | If your min-ed-launcher config also starts companion apps (its own process list) → LAUNCH → exit | Record duplicates or conflicts. Test with that option on and off. |
| CA-17 | P2 | R | A tool with its own "close when the game exits" feature | No errors when EDLS finds it already gone at teardown. |
| CA-19 | P2 | A | "Start tools minimized" on → a card's START (EDHM-UI too, if you have it: it goes through the UAC prompt). Then turn it off → SAVE → LAUNCH. | START starts the tool minimized as well (where the tool allows it, see check g). A tool that was already running isn't minimized or moved. With the toggle off, tools open normally, as before. |

### 8.16 Performance (PF)

Use Task Manager → Details → the `python.exe` whose command line ends in `main.py`.

| ID | Pri | Env | Steps | Expected |
| --- | --- | --- | --- | --- |
| PF-01 | P1 | A | Idle for 5 minutes (EDLS checks processes every 5 s) | CPU close to 0% (well under 1%). Note the memory use. |
| PF-02 | P1 | R | During a session with all cards pulsing (checks every 2 s) | Average CPU under about 2%. Record the number. |
| PF-03 | P1 | R | Compare game FPS with EDLS watching vs. without EDLS, in the same spot (e.g. docked) | No visible difference. No stutter every 2 s. |
| PF-07 | P3 | A | Minimise EDLS for 10 minutes, then restore it | Cards are up to date the moment it's restored. |
| PF-04 | P2 | R | Memory at the start vs. after a 3-hour session | Little or no growth. Record both numbers. |
| PF-05 | P2 | A | Time from `python main.py` to a usable window, and to the first status check finishing | Record both. |
| PF-06 | P3 | A | SrvSurvey enabled on a PC with a big `%LOCALAPPDATA%\Apps\2.0` folder → LAUNCH | Record how long preflight takes. |

### 8.17 Keyboard and accessibility (AX)

| ID | Pri | Env | Steps | Expected |
| --- | --- | --- | --- | --- |
| AX-01 | P1 | A | Main window: press Tab repeatedly, then Space; also Shift+Tab | Tab visits the card buttons, the "report issue" link and the four bottom buttons. The focused bottom button shows a dashed inset outline, a card button a dashed border. Space presses it; Enter on the link opens it. Clicking with the mouse leaves no outline (R-20 fix). |
| AX-02 | P1 | A | Do a full Settings change with the keyboard only: name, every toggle, add/edit/remove/tick an app, Save | Everything is reachable and visibly focused (SE-13, SE-53). Nothing needs the mouse (R-20 fix). |
| AX-03 | P3 | A | Esc in Settings and in the add/edit window. Enter in the add/edit window. | Esc cancels. Enter saves (with the same name checks as clicking Save). |
| AX-04 | P3 | A | Status without relying on colour | The state text alone tells you the state. Grey text on dark cards is readable. |
| AX-05 | P3 | A | Windows Narrator over the buttons and cards | Record what's read out. |
| AX-06 | P3 | A | At 100% scaling on a 24" 1080p screen, read the small card buttons and the badges | Record anything too small to read. |

### 8.18 Other edge cases (ED)

| ID | Pri | Env | Steps | Expected |
| --- | --- | --- | --- | --- |
| ED-01 | P2 | A | Sign out of Windows, restart, and shut down with EDLS open (idle, and during a session) | Windows doesn't hang on "this app is preventing shutdown". The next start is normal. |
| ED-02 | P3 | R | Remote Desktop or fast user switching mid-session | The session carries on. |
| ED-03 | P2 | A | Change the clock or time zone mid-session | No effect. |
| ED-04 | P3 | A | With your antivirus active | Record any alerts about EDLS starting or closing programs. |
| ED-05 | P2 | A | On a **copy** of the repo, delete one at a time: `assets\EUROCAPS.TTF`, `assets\ShareTechMono-Regular.ttf`, `assets\window-bg.svg`, `assets\title-mark.svg`, `assets\icon.ico` → run each time | EDLS always starts. Fonts fall back to system fonts, the background falls back to a plain colour, and the banner shows without the title mark. No crash. |
| ED-06 | P3 | A | Non-English Windows display language (e.g. German) | Works. Default tool paths are still found. |
| ED-07 | P3 | A | `%LOCALAPPDATA%` on a different drive or redirected | Works. |
| ED-08 | P3 | A | Open `launcher.log` in a program that locks files (e.g. Excel) while EDLS runs and logs | Record. EDLS should keep working, even if the log stops updating. |
| ED-09 | P3 | R | Steam in offline mode. Switching Steam accounts. | Record. |
| ED-10 | P2 | A | Click every button and card at random for 30 seconds, idle and during a session | No crash and no traceback. |
| ED-11 | P3 | A | Leave EDLS open and idle for 24 hours | Still responsive. Memory roughly flat. |

### 8.19 Documentation (DC)

| ID | Pri | Env | Steps | Expected |
| --- | --- | --- | --- | --- |
| DC-01 | P2 | A | Follow the README install and run steps exactly, on a clean PC | They work as written. |
| DC-02 | P2 | A | Compare the README's feature list with the app | Report every mismatch (it was updated for live status, the close-EDLS toggles, the app-list lock, one-copy-only and the update check). |
| DC-03 | P3 | A | Proofread the welcome text, Settings labels and log messages | Report typos and anything unclear. |

### 8.20 Update check (UP)

EDLS asks GitHub for the latest release about a second after it starts. It never downloads
or installs anything: if a newer version is out, the update badge explains the steps and
opens the release page. To see the badge, run an older EDLS: from source (§3.2) with
`APP_VERSION` near the top of `config.py` set lower, e.g. `"1.0.0"`. Put it back
afterwards.

| ID | Pri | Env | Steps | Expected |
| --- | --- | --- | --- | --- |
| UP-01 | P1 | A | Run the latest version → start EDLS | No update badge and nothing about updates in the activity log. `launcher.log` has "Update check: EDLS &lt;version&gt; is the latest." |
| UP-02 | P1 | A | Run an older version → start EDLS. Repeat in light mode. | About a second after the panels appear, a green "UPDATE x.y.z AVAILABLE" badge shows at the right end of the CMDR name's row (green in both themes). The CMDR name stays centred under the title and the header grows only a few pixels. Repeat with a 36-character CMDR name (the most Settings takes): the name moves left rather than run into the badge, and stays clear of the ringed-planet mark. Only a name made mostly of wide letters (MMMM…, WWWW…) may reach it. The log says "EDLS x.y.z is available - click the UPDATE badge next to your CMDR name." in green (pale green in light mode). |
| UP-03 | P1 | A | Click the update badge. Repeat in light mode, and reach the badge with Tab → Enter. | A popover: "U P D A T E   A V A I L A B L E", "EDLS x.y.z is out - you have …", then how to update (download `EDLS-Setup-x.y.z.exe` from the release page and run it). Two buttons: LATER and RELEASE PAGE. Enter and Esc both mean LATER: nothing happens and the badge stays. Record anything in the text that's unclear. |
| UP-04 | P1 | A | Update popover → RELEASE PAGE | Your browser opens the latest release's GitHub page, showing that version with its installer under Assets. The badge stays. |
| UP-05 | P1 | A | With EDLS open, follow the popover's steps: download the installer and run it. Needs a published release newer than the installed EDLS. | Same as IN-02: setup closes EDLS, and Steam, the game and the tools stay open. Let setup start EDLS at the end: the version chip shows the new version, there's no update badge, and your settings and app list are unchanged. |
| UP-06 | P1 | A | Settings → "Check for EDLS updates at startup" off → Save → restart an older version | No badge. `launcher.log` has no "Update check" line at all. Turn it back on → restart: the badge is back. |
| UP-07 | P2 | A | Disconnect from the internet → start EDLS | No popup and nothing in the activity log. `launcher.log` has "Update check failed: …". Everything else works. |

---

## 9. Interaction sequences

These check "while EDLS is busy doing X, the user does Y". Use the harness. How to hold
each state:

| State | How to get there | How long it lasts |
| --- | --- | --- |
| **Waiting for game** | LAUNCH, and don't open the fake game | Until you open it |
| **In session** | LAUNCH → open the fake game | Until you close it |
| **Game-only start** | Elite card START, no fake game | 60 s |
| **Waiting after SHUTDOWN** | In session → SHUTDOWN ("Close EDLS after SHUTDOWN" off) | Until you close the fake game |
| **Countdown** | In session → close the fake game ("Close EDLS when the game exits" on) | 5 s |
| **Steam starting** | Steam closed → LAUNCH | Up to about 20 s |
| **Steam stopping** | Steam open, no game → Steam card STOP | Up to about 15 s |
| **Idle** | Nothing running in EDLS | — |

| ID | Pri | While… | Do… | Expected |
| --- | --- | --- | --- | --- |
| SQ-01 | P2 | Waiting for game | Open and close a dummy yourself | The Elite card stays "Waiting..."; the dummy's card follows reality. |
| SQ-02 | P2 | Waiting for game | SHUTDOWN | Closes per scope. Still waiting (up to the 60 s limit). |
| SQ-03 | P2 | Waiting for game | Settings → change the CMDR name → Save | The Elite card keeps "Waiting..." (R-14 fix). Opening the fake game continues the session normally. |
| SQ-04 | P2 | Waiting for game | Switch theme | As SQ-03. LAUNCH stays "RUNNING...". |
| SQ-05 | P1 | Waiting for game | Settings → try to untick Dummy A | Not possible: the list is locked (SE-51). Dummy A starts with the session as normal. |
| SQ-06 | P1 | Waiting for game | Settings → try to + ADD a dummy | Not possible: the lock popup (SE-51). |
| SQ-07 | P1 | Waiting for game | Steam card button / Elite card button | Steam: "Launch sequence already running.". Elite (greyed STARTING): "The game is already starting.". |
| SQ-08 | P2 | Waiting for game | A companion app's START | It starts now and is closed at session end (both scopes). |
| SQ-09 | P1 | Waiting for game | Close EDLS → then open the fake game | EDLS closes cleanly. Opening the fake game afterwards starts no tools. |
| SQ-10 | P3 | Waiting for game | Toggle AUTO START | Saved. Nothing else happens. |
| SQ-11 | P1 | In session | Close a dummy yourself | Its card turns Offline within about 2 s, with a "… stopped running." log line. |
| SQ-12 | P2 | In session | Open an unlaunched dummy yourself → end the session | Its card turns Online by itself. Closed or not at the end according to scope (SC-04). |
| SQ-13 | P2 | In session | STOP a dummy → START it → close it yourself | See CC-16 (R-16 fix). |
| SQ-14 | P2 | In session | Change "Close EDLS when the game exits" in Settings → close the fake game | The value you just saved decides whether the countdown runs. |
| SQ-15 | P1 | In session | SHUTDOWN | See SC-12 (toggle off) and SC-18 (toggle on). |
| SQ-16 | P2 | In session | Switch theme | The session continues; cards keep their states. Closing the fake game still tears down and counts down. |
| SQ-17 | P1 | In session | Close EDLS → reopen it → close the fake game | Everything stays open after the close. The reopened EDLS shows the right states. Closing the fake game then only turns its card Offline: no teardown (no session), and EDLS stays open. |
| SQ-18 | P2 | In session | Open Settings → close the fake game while Settings is open | The countdown runs behind Settings. When it ends, EDLS stays open ("EDLS stayed open - a dialog was open.") and LAUNCH comes back (R-22 fix). |
| SQ-19 | P2 | In session | Open + ADD (or Settings → it's locked, so use OPEN LOG FOLDER instead) and leave a window open → close the fake game | Same as SQ-18: EDLS stays open behind the open window (R-22 fix). |
| SQ-20 | P2 | In session | Close the fake game and reopen it within a second | Record (R-30). The teardown still runs and the countdown starts; the new "game" has no tools. |
| SQ-21 | P2 | In session | Press SHUTDOWN and close the fake game at the same moment | No errors. Each tool is closed once (one round of "Stopping …" lines), one countdown runs, and EDLS closes once (R-42 fix). |
| SQ-22 | P1 | Game-only start | Click LAUNCH | Greyed; logs "The game is already being started from its card - wait for it to finish."; nothing launches (R-09 fix). |
| SQ-23 | P2 | Game-only start | Save Settings / switch theme | The Elite card keeps "Waiting..." (R-14 fix). |
| SQ-24 | P3 | Game-only start | Steam card START | Ignored silently. Record whether you'd expect a message. |
| SQ-25 | P2 | Game-only start | Open the fake game → later close it | Running. No tools start. After closing it the card goes Offline within about 5 s (R-13 fix), then START is held for 3 s. |
| SQ-26 | P2 | Game-only start | Close EDLS | Clean exit. |
| SQ-27 | P3 | Steam starting | Wait | The Steam card stays "Launching..." until Steam appears (never flips to Offline meanwhile). |
| SQ-28 | P2 | Steam starting | Close EDLS | Steam keeps starting. No game launch request is sent afterwards. |
| SQ-29 | P3 | Steam stopping | LAUNCH | Record. EDLS may see Steam as still running and ask it for the game just as Steam exits. |
| SQ-30 | P3 | Steam stopping | Press the Steam card button again | Ignored. |
| SQ-31 | P2 | Idle | LAUNCH → close EDLS immediately | No tools start after the close. |
| SQ-32 | P2 | Idle | Settings → change things → Cancel → LAUNCH | LAUNCH uses the old settings. |
| SQ-33 | P2 | Idle | Settings → change things → Save → LAUNCH straight away | LAUNCH uses the new settings. |
| SQ-34 | P1 | Waiting after SHUTDOWN | Open Settings | The app list is still locked (the session lasts until the game closes). Other settings can change. |
| SQ-35 | P2 | Waiting after SHUTDOWN | Close the fake game | "No tools running." → countdown (or "Session ended." with the toggle off). |
| SQ-36 | P2 | Waiting after SHUTDOWN | Press SHUTDOWN again | "No tools running." and the "quit in-game" line again. Nothing breaks. |
| SQ-37 | P2 | Countdown | Click STAY OPEN (the LAUNCH key) | "EDLS will stay open." Nothing launches; the key goes back to LAUNCH and EDLS stays open (R-36 fix). |
| SQ-38 | P2 | Countdown | Open Settings | It opens. When the countdown ends, EDLS stays open behind it ("EDLS stayed open - a dialog was open.") (R-22 fix). |
| SQ-39 | P2 | Countdown | Close the window yourself | See LS-29. |
| SQ-40 | P3 | Countdown | Open a dummy's card START | It starts; EDLS still closes on time, and the dummy keeps running. |

---

## 10. End-to-end journeys

Play these as a real user would. Note anything awkward, not just failures.

| ID | Pri | Env | Journey | Watch for |
| --- | --- | --- | --- | --- |
| JR-01 | P1 | R | **New CMDR.** Fresh install → setup window → pick your tools → LAUNCH → play 15 min → exit to desktop → next day open EDLS again | Setup is understandable. Everything closes properly. Settings are remembered. |
| JR-02 | P1 | R | **Daily driver.** AUTO START on → each day just open EDLS → play → exit. Repeat on 3 days. | It's hands-off every time, and EDLS closes itself at the end. |
| JR-03 | P1 | R | **Pre-opened tools.** Open EDMC yourself → open EDLS → LAUNCH → play → exit | EDMC is closed at the end, under "launched only" too: LAUNCH took it over. |
| JR-04 | P2 | R | **Oops.** LAUNCH → close EDLS by accident mid-game → reopen → LAUNCH to re-attach → exit | No harm to the game. Re-attaching works without a second launch request (R-02 fix). Teardown happens. |
| JR-05 | P2 | R | **Tinkerer.** Mid-session: try to add a tool (locked: note how the popup reads), switch theme, change scope and the close-EDLS toggles → keep playing → exit | The lock is understandable. The teardown and close behaviour match your final settings. |
| JR-06 | P2 | R | **Game only.** Elite card START → later START two tools from their cards → exit the game → SHUTDOWN | Nothing closes when the game exits (the Elite card just goes Offline). SHUTDOWN closes the card-started tools. |
| JR-07 | P2 | R | **Broken launch.** Make the game fail to start → LAUNCH → wait for the 60 s cancel → fix the cause → LAUNCH | Clear message, no need to restart EDLS (R-10 fix). |
| JR-08 | P2 | R | **Laptop.** 1024×768, 1366×768 or 150% scaling, light theme → a full session | Everything reachable and readable (R-18 fix). |
| JR-13 | P2 | R | **Stay-open player.** Turn "Close EDLS when the game exits" off → play → exit → relaunch from EDLS → exit again | EDLS stays open between sessions, and each session starts and tears down cleanly. |
| JR-14 | P2 | R | **Quick quit.** Turn "Close EDLS after SHUTDOWN" on → mid-game SHUTDOWN → keep playing → exit the game | EDLS and the tools are gone straight away; the game keeps running until you exit it. |
| JR-09 | P2 | R | **Uninstalled tool.** Uninstall a companion app → LAUNCH → see the warning → remove it in Settings | Clear warning. Stays removed after restart. |
| JR-10 | P2 | R | **Moved tool.** Reinstall a tool to a new folder → LAUNCH (Not found) → EDIT → Browse… → LAUNCH | Easy to fix. Works afterwards. |
| JR-11 | P2 | R | **Mission stacker.** Exit to desktop and relaunch several times in an evening | Record how EDLS fits this play style (R-30). |
| JR-12 | P3 | A | **Keyboard only.** JR-01 without a mouse | Record every blocker (R-20). |

---

## 11. Environments

| Environment | What to run |
| --- | --- |
| **Primary:** Windows 11, 1920×1080 at 100%, Steam licence, real game | Everything |
| Windows 10 22H2 | Smoke, §7, §8.4, §8.6, §8.14, basic §8.9 |
| Small screens: 1024×768 (the smallest supported), 1366×768, or 1920×1080 at 150% | §8.3, JR-08 |
| 4K at 150–200% | UI-06, UI-09 |
| Two monitors with different scaling | UI-08 |
| Python 3.11 **and** the newest Python 3 release | Smoke on both |
| Installed build (`EDLS-Setup-<version>.exe`) | §3.5, Smoke (minus SM-01), §8.6, §8.14 |
| Odyssey and Horizons/Legacy (if owned) | LS-21 |
| Light theme | Smoke, §8.3, §8.11 |
| Non-English Windows (optional) | ED-06 |

---

## 12. Known risk areas

These come from reading the code. Each row has a **Status**:

- **Fixed — verify**: a fix went in. Rerun the listed tests and report **Fixed** or
  **Still broken**.
- **Open**: a suspicion, not a confirmed bug. Report **Confirmed**, **Not reproduced**
  or **Not tested**, with the test ID you used.

A risk leaves this table once its fix is verified. The "(R-xx fix)" notes in the tests
mark where each one was checked.

| ID | What may be wrong | Status | Check with | Severity if confirmed |
| --- | --- | --- | --- | --- |
| R-07 | A renamed SrvSurvey works until restart, then shows "Not found". | Fixed — verify (the default it came from is saved) | SE-38 | Minor |
| R-19 | CMDR names and app names that look like HTML (`<b>…</b>`) may be shown as formatting instead of text. | Fixed — verify (shown as plain text) | SE-10 | Cosmetic |
| R-20 | Settings toggles may not work from the keyboard, and keyboard focus may be invisible on the main buttons. | Fixed — verify | AX-01, AX-02, SE-13, SE-53 | Minor |
| R-22 | If the game exits while Settings or a file picker is open, EDLS tries to close itself behind the open window (now after the 5 s countdown). | Fixed — verify (EDLS stays open) | SE-46, SQ-18, SQ-19, SQ-38 | Minor to Major |
| R-25 | The activity log may jump to the bottom on every new line, even while you're scrolled up reading. | Fixed — verify | LG-04 | Cosmetic |
| R-26 | First-run ticks all 12 default apps, so new users with few tools get a pile of "not found" warnings. | Fixed — verify (only installed apps are ticked) | FR-01, FR-07, FR-08 | Minor |
| R-30 | Any exit of the game ends the session. If the game is restarted (relog or min-ed-launcher restart), the tools are already closed. | Open (design question; the "Close EDLS when the game exits" toggle softens it) | LS-20, SQ-20, JR-11 | Design question |
| R-31 | EDLS can see other Windows users' programs, so cards may show someone else's game or tools as running. | Open | RF-07 | Minor |
| R-34 | With AUTO START on but setup unfinished, the log may say "Auto-start enabled — launching..." although nothing launches. | Fixed — verify | FR-04, AS-04 | Cosmetic |
| R-35 | The 60 s limit on the game appearing may be too short on a slow PC's cold start (Steam starting, updates, min-ed-launcher login). | Open (new) | LS-31, LS-02, AS-03 | Minor (press LAUNCH again) |
| R-36 | The 5 s close countdown can't be cancelled, except by closing EDLS yourself. If you change your mind, you have to reopen EDLS. | Fixed — verify (STAY OPEN) | LS-35, SC-22, SQ-37 | Design question |
| R-37 | The "X is now running." / "X stopped running." lines may get noisy for tools that restart themselves (updaters, launcher stubs). | Open (new) | RF-01, CA-13, CA-15 | Cosmetic |
| R-39 | The UAC prompt for an admin tool appears just as the game starts, and may take a fullscreen game out of focus. | Open (new) | LS-34 | Minor |
| R-40 | After you answer Yes, an admin tool may take longer than 3 s to appear. If it does, its card shows Failed until the status check finds it (within about 5 s). | Open (new) | LS-11, CC-20 | Cosmetic |
| R-42 | Two closes of one tool could run at once (STOP double-clicked, STOP during SHUTDOWN, SHUTDOWN as the game exits): duplicate log lines, two close countdowns, and for an admin tool two UAC prompts. | Fixed — verify (one start or stop per tool at a time; teardowns and countdowns never overlap) | CC-06, CC-21, CC-22, SC-13, SQ-21 | Minor |
| R-43 | Closing EDLS while it was still closing tools ended that mid-way: a tool that ignored being asked was never force-closed. | Fixed — verify (EDLS finishes out of sight, at most 10 s, with no new UAC prompt) | SC-23, SC-24 | Major |
| R-44 | SHUTDOWN while LAUNCH was still starting tools closed what was up, then LAUNCH started the rest. | Fixed — verify (the rest stay unstarted) | LS-36 | Minor |

---

## 13. Reporting bugs

**Severity**

| Level | Use it for |
| --- | --- |
| **Blocker** | EDLS closes Steam or the game. EDLS won't start. A companion app loses data. Anything unsafe. |
| **Critical** | The core flow is broken (LAUNCH, teardown or SHUTDOWN does the wrong thing), the wrong apps are closed, or EDLS hangs. |
| **Major** | A feature misbehaves, or a status is misleading, but there's a workaround. |
| **Minor** | Small functional or usability problem. |
| **Cosmetic** | Visual only. |

**Template**

```
Title:        [Test ID] one-line summary
Build:        commit abc1234 or installer EDLS-Setup-1.3.0, version badge 1.3.0
Environment:  Windows 11 23H2, 1920x1080 @ 100%, Python 3.11.x,
              theme Dark, scope "launched only", real game / harness
Steps:        1.
              2.
Expected:
Actual:
Frequency:    always / sometimes (x out of y) / once
Severity:     Blocker / Critical / Major / Minor / Cosmetic
Attachments:  launcher.log, settings.json, screenshot or video, terminal output
```

**Attach** `launcher.log` (plus `launcher.log.1` if the problem is older), `settings.json`,
anything printed in the terminal (especially tracebacks), and a screenshot or video.
Both files contain your CMDR name and file paths that include your Windows user name, so
edit those out if you prefer.

**Where:** the "report issue" link in EDLS (GitHub issues), or straight to the developer.

**Results sheet:** one line per test, e.g. `LS-04, Pass` / `SC-03, Fail, #42` /
`LS-21, N/A (no Horizons)`.

---

## 14. Sign-off

- [ ] Smoke test passes on Windows 10 and Windows 11
- [ ] Every P1 test passes in the primary environment
- [ ] §8.6 (Steam and the game are never closed) passes 100% with the real game
- [ ] §8.14 (closing EDLS safely) passes
- [ ] No open Blocker or Critical bugs
- [ ] §7 regression checks RG-09 to RG-19 pass
- [ ] §3.5 installed-build checks (IN-01 to IN-05) pass on the release installer
- [ ] Every "Fixed — verify" R-nn is reported Fixed or Still broken
- [ ] Every "Open" R-nn is marked Confirmed, Not reproduced, or Not tested
- [ ] Clean-up done: harness removed, `elite_app_id` back to `359320`, EDLS data restored
