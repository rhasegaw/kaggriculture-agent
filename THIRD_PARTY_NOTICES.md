# Third-party notices

## Kaggle Environments

The optional runtime dependency is `kaggle-environments==1.32.7`. It is not vendored in this repository. Engine synchronization is on by default when the dependency is installed; the base smoke test disables it and uses only the Python standard library.

- Project: [Kaggle/kaggle-environments](https://github.com/Kaggle/kaggle-environments)
- License: [Apache License 2.0](https://github.com/Kaggle/kaggle-environments/blob/master/LICENSE)
- Copyright notice in the upstream package: Kaggle Inc.

The game constants in `main.py` are documented as mirrors of the public Kaggle environment. No upstream Kaggle source file is copied into this archive. Anyone installing the optional dependency must comply with its Apache-2.0 license and any licenses of its transitive dependencies.

## Retained source provenance

`main.py` is the retained user-owned source snapshot, © 2026 Ryo Hasegawa. The original controller and documentation are licensed under [Apache-2.0](LICENSE). The optional Kaggle runtime dependency remains governed by its separate upstream notice above.
