These are "wishlist" items that are perhaps smaller and if we can fold them into other work it would be helpful. This wishlist can be committed at any time.

- Show bounded box for the last scan area
- Error bars in master fit, showing the width of the phonon energy error
- Better symbols with clear movement from individual (resolved/not) to master scans
    1. Small green circle for valid individual fit
    2. Larger hollow green circle for the current master fit center that matches target conditions
    3. Hollow red diamond for the current master fit that does NOT match target conditions
    4. Hollow blue square for a master fit that is certified
- Master scans give total number of neutrons for the master scans, i.e. how much we actually "spent" to get the result
- User-facing log cleanup, e.g. no "start" "completed" as separate lines
- Connecting to TAVI seems to be slow with a new workspace, weird when it was connected before
- If a folder doesn't exist when saving a workspace, create it with that name
- Data point error bars should be Poisson-correct and asymmetric, so e.g. a count of 1 gives far smaller error bars on the negative side than the positive side
- Get weird black-and-white color palette for some scans? Maybe it's the first one?
- Take crystollographic energy to get BZ sizes (not small)
- Ensure that the GUI is kept updated, so e.g. it updates with scans as they finish not just as fits finish
- `tests/gui/test_browser.py::test_load_workspace_twice_no_menu_leak` is PAUSED (`@pytest.mark.skip`, 2026-07-21): it hard-crashes the interpreter (exit 139, access violation) even run alone, and took the whole GUI suite down with it — the other ~691 GUI tests were never executing. Needs real diagnosis, not deletion: it guards against a menu leak on repeated workspace loads, and loading a workspace twice is a routine operator action, so the crash may be a genuine `load_workspace` teardown bug rather than a test artefact. Suspect the test's global `QApplication.sendPostedEvents(None, DeferredDelete)`, which drains deferred deletes for every object in the process.
- GUI tests now stub the backend health probe offline via an autouse fixture in `tests/gui/conftest.py`. Kept deliberately though it was not the cause of the crash above: without it, constructing any `MainWindow` in a test fires real `urlopen` calls at the operator's live LLMM/TAVI ports, so results depend on which services happen to be running.