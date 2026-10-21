# Retiring a site

Add a `retirement` table to the site.

```toml
[adapters.chameleon.sites."KVM@TACC".retirement]
retires_on = 2027-01-15
warn_from = 2027-01-01
notice = "KVM@TACC retires on 2027-01-15. Consider the VM flavors at CHI@TACC."
retired_notice = "KVM@TACC has been retired. Try the VM flavors at CHI@TACC."
```

| Field | Use |
|---|---|
| `retires_on` | From this day, the site leaves the catalog, inventory, and every list of sites. |
| `warn_from` | Descriptions whose lease ends on or after this day get `notice`. |
| `notice` | The chat posts it after the description. `ccx describe` prints it on stderr. |
| `retired_notice` | The chat and `ccx` show it when a request or a build names the site after `retires_on`. |
