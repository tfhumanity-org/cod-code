"""[PSEUDO] Things that cannot sensibly be real stdlib Python.

Each module here is *documented pseudocode in Python form*: Protocol interfaces
for the native/OS seams, real implementations of the pure policy parts, and a
simulated backend so the policy can be tested offline.

* ``sandbox_profiles`` - OS sandbox profile generation + egress policy (spec 04 §4.8-4.9, WP3)
* ``computer_use``     - computer-use policy server + native seams (spec 09, 10, WP5)
* ``desktop_host``     - Electron desktop host algorithms (spec 00, 08, WP5)
"""
