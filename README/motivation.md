# Motivation

Use this launcher when you want hard infrastructure boundaries instead of a soft, inconsistent permission UX.

Local screenshots used in this note live alongside this file under `README/`.

## 1. A denied write should stay denied

If an agent is told it cannot write outside the workspace, it should not work around that by generating a helper script and executing it through the shell. That breaks trust in the tool.

This repo takes the opposite approach: the trust boundary is the container plus the explicit bind-mount set. The agent gets the repo, the configured host paths, and nothing else.

Reference:
- https://x.com/evisdrenova/status/2040174214175723538

![Evis Drenova post about permission bypass](motivation_3.png)

## 2. Permission prompts should not fight the operator

Once the operator has already defined the allowed surface area, the tool should stop asking about routine writes. Repeated prompts for temporary files or previously allowed paths destroy flow without improving safety.

This repo pushes the decision to the environment boundary instead:

- the target repo is mounted read/write at its real host path
- selected folders are mounted read-only at their real host paths. assume they will be read
- normal in-container scratch space such as `/tmp` stays usable without ceremony

Reference:
- https://x.com/MythThrazz/status/2040394930200170738?s=20

![Marcin Dudek post about repetitive permission prompts](motivation_2.png)


## What This Repo Is Optimizing For

- path fidelity instead of fake `/workspace` paths
- explicit mounts instead of hidden permission hacks
- low-friction operation after the boundary is chosen
- trusted-repo full-access workflows on a dedicated machine

The core thesis is simple: enforce safety at the container boundary, then let the agent operate normally inside that boundary.
