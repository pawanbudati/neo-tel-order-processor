# GitHub Actions deployment to GCP

The `Deploy consumer` workflow deploys every push to `main` and can also be started manually from the GitHub Actions tab. It syncs this repository to the VM, installs Python requirements, and restarts the `neo-tel-order-processor` systemd service.

## Configure GitHub

In this repository's **Settings → Secrets and variables → Actions**, add:

**Secrets**

- `GCP_VM_SSH_KEY`: private SSH key for a VM account that can write to the deployment directory and run the required `sudo` systemd/install commands without a password.
- `GCP_VM_KNOWN_HOSTS`: the VM's verified SSH host key line(s), obtained and fingerprint-checked out of band.

**Variables**

- `GCP_VM_USER`: Linux account matching the public key installed on the VM.
- `GCP_VM_SSH_PORT`: optional; defaults to `22`.
- `CONSUMER_DEPLOY_PATH`: optional; defaults to `/home/<GCP_VM_USER>/apps/neo-tel-order-processor`.

`GCP_VM_HOST` and `GCP_VM_USER` can each be stored as either a secret or a variable; the workflow checks both.

The VM needs Python 3 and `sudo` access. If `rsync` is missing, the workflow installs it using the VM's `apt-get`, `dnf`, or `yum` package manager. Restrict SSH ingress to trusted sources where practical. The workflow does not transfer local secrets or generated state.

## Prepare the VM once

Create the deployment directory and configure the app before enabling automatic deploys:

```bash
mkdir -p "$HOME/apps/neo-tel-order-processor"
```

Create `consumer/.env` on the VM using `.env.example` as a reference. Set the Kotak credentials and Kafka connection details there. Keep the file private and outside Git. The deploy account needs passwordless sudo for `install`, `systemctl daemon-reload`, `systemctl enable`, `systemctl restart`, and `systemctl status`, because the workflow installs and restarts a system service.

After the first workflow run, inspect logs with:

```bash
sudo journalctl -u neo-tel-order-processor -f
```

Use `sudo systemctl status neo-tel-order-processor` to check service state, or `sudo journalctl -u neo-tel-order-processor --since today` to review today's logs. Producer logs are available with `sudo journalctl -u neo-tel-signal-producer -f`; Kafka container logs on the producer VM use `sudo docker logs -f trading-kafka`.

Protect `main` if deployments should happen only after review. A consumer restart clears its in-memory pending-order tracking, and the consumer can place live orders after it reconnects. Review the timing of updates accordingly.
