# test_ci_mode init and #419 (2026-10-27)

#419 makes fa init ask once about upstream reports. The locked test_ci_mode init fixture failed on any question; it now answers 'n' to that question and still forbids a checks-mode question. Neutral on main.
