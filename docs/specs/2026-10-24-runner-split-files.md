# Narrow runner Files after #143

#143 split runner.py into runner.py and run_* modules. Waiting issues declared the globs `src/foragent/runner*.py` and `src/foragent/run_*.py`, which overlap with every runner change and block lane clones. Each issue now lists the modules its goal and locked test need (chosen inclusively; a missing module would make the overlap check unsafe). Goals, tests and checks are unchanged.
