# DMOCRP

The DemoCorp group homepage: it introduces DMOCRP's two companies, DMOBNK (`../dmobnk`) and DMOSHP (`../dmoshp`), plays the promo video, and links to whichever of their apps run on the same cluster.

- **Served at:** `http://dmocrp.<address>.sslip.io/`. The app creates its own Ingress, owned by its Deployment, and points it at the cluster, the same way the other apps do (`common/ingress_host.py`, a copy kept by `./sync-shared.sh`). To set a host or class, use the `host` / `ingressClass` keys of a `dmocrp-site` ConfigMap.
- **Video:** `media/democorp-promo.mp4` and its poster live at the repo root, outside the Fleet bundle, because they're too big for a ConfigMap. The pod's init container downloads them from GitHub when it starts. A cluster without internet access shows the poster and a link instead.
- **Links:** it reads the Ingress hosts in `dmobnk` and `dmoshp` (read only). Apps that aren't deployed show as "Not on this cluster".

**Deploying.** With Fleet, add a GitRepo with path `dmocrp`. It deploys to every cluster the GitRepo targets, with no labels. Without Fleet, run `dmocrp/deploy.sh`.
