# Installation and updates

## Requirements

Home Assistant 2026.11.0 or newer is required. Home Assistant installs the
integration's declared Python dependency automatically. This is a custom
integration, not an app/add-on and not a separate Docker container.

The Home Assistant configuration directory is the directory containing
`configuration.yaml`. It is normally `/config` inside Home Assistant OS or a
Home Assistant container; a container host's mounted directory can differ.

## Marketplace

1. Open **Settings → Marketplace** as an administrator.
2. Search for **Govee Native** and open its catalog page.
3. Install it and restart Home Assistant.
4. Open **Settings → Devices & services → Add integration → Govee Native**.

If the catalog has not updated on your instance, select **Add from link** and
enter `https://github.com/Kosztyk/govee_native`. Connect GitHub if prompted.
If asked for a content type, choose **Integration**.

Home Assistant 2026.11 includes the Marketplace, which replaces HACS. This
integration's minimum version is 2026.11; installing legacy HACS on an older
Home Assistant version does not remove that minimum requirement.

Official instructions: [Marketplace custom repositories](https://www.home-assistant.io/integrations/marketplace/#custom-repositories).

## Manual GitHub ZIP installation

1. Visit [Releases](https://github.com/Kosztyk/govee_native/releases).
2. Download **Source code (zip)** from the release you want to install.
3. Extract the archive on your computer.
4. Create `custom_components` in your Home Assistant configuration directory
   if it does not already exist.
5. Copy the extracted `custom_components/govee_native` folder into it.
6. Confirm that `/config/custom_components/govee_native/manifest.json` exists.
7. Restart Home Assistant and add **Govee Native** through Devices & services.

You do not need a `govee_native:` section in `configuration.yaml`.

## Update an existing Govee Native installation

Keep your existing integration entry. Its domain remains `govee_native`, and
the existing account and entity unique IDs are preserved.

- **Marketplace:** install the available update and restart Home Assistant.
- **Manual:** save a backup of the current integration folder, replace it with
  the new `custom_components/govee_native` folder and restart Home Assistant.

If DIY scenes stopped working after rediscovery in 0.5.2, upgrade to 0.5.3
and try the same scene after restarting Home Assistant.

Avoid mixing manual file replacements with Marketplace-managed updates unless
you intend the next Marketplace update to replace those files.

## Existing legacy Govee installations

The legacy `govee` integration and Govee Native use different domains. Govee
Native does not automatically import or delete the legacy integration, rename
its entities or retarget existing automations.

Add Govee Native, check that its controls work, then choose which entities your
dashboards and automations should use. If you retire the legacy integration,
check its consumers first. Account setup is only needed when moving from a
different integration, not when updating an existing Govee Native entry.

## Roll back

For a manual installation, restore the saved integration folder and restart
Home Assistant. To roll back a Marketplace update, restore a Home Assistant
backup from before the update.
