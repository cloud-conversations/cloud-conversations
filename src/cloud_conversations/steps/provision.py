"""Provisioning step functions."""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone

import cloud_conversations.db.store as store
from cloud_conversations import audit
from cloud_conversations.adapter.errors import (
    AdapterTimeoutError,
    CapacityError,
    FatalError,
    TransientError,
)
from cloud_conversations.adapter.protocol import Lease
from cloud_conversations.config import ConfigError, resource_name
from cloud_conversations.environment.install import InstallError, InstallSettings, install
from cloud_conversations.environment.items import SETUP_KEYS
from cloud_conversations.schema.description import reservation_kind_of
from cloud_conversations.schema.runs import Connection
from cloud_conversations.steps import retry
from cloud_conversations.steps.context import StepContext, StepError, finish_by
from cloud_conversations.steps.failure import FailureKind, out_of_addresses, unusable_host
from cloud_conversations.steps.failure import record as record_failure
from cloud_conversations.steps.idempotency import derive_key


def _started(ctx: StepContext, step_id: str, action: str, args: dict) -> None:
    store.append_event(
        ctx.run_id,
        "runner",
        "step_started",
        {"step_id": step_id, "action": action, "args": args},
        owner_id=ctx.owner_id,
    )


def _failed(
    ctx: StepContext, step_id: str, action: str, failure_kind: FailureKind, exc: Exception
) -> None:
    record_failure(ctx.run_id, ctx.owner_id, step_id, action, failure_kind, str(exc))


def _heartbeat(ctx: StepContext) -> None:
    store.touch_heartbeat(ctx.run_id)


def _until(moment: datetime) -> float:
    """Seconds from now until a moment, or zero once it has passed."""
    return max(0.0, (moment - datetime.now(timezone.utc)).total_seconds())


def reserve(ctx: StepContext, args: dict) -> dict:
    """Create one lease with the compute reservation and a floating IP."""
    step_id: str = args["step_id"]
    try:
        _started(ctx, step_id, "reserve", args)
        node_type: str | None = args.get("node_type")
        flavor_name: str | None = args.get("flavor")
        duration_hours: float = float(
            args.get("duration_hours") or ctx.requirements.duration_hours
        )

        flavor_id: str | None = None
        if flavor_name:
            flavor = ctx.adapter.find_flavor(ctx.site, flavor_name)
            if flavor is None:
                exc: Exception = ValueError(
                    f"No flavor named {flavor_name!r} at {ctx.site}"
                )
                _failed(ctx, step_id, "reserve", "description", exc)
                raise StepError(exc, "description")
            flavor_id = flavor.flavor_id

        requested: str | None = args.get("start_at")

        ikey = derive_key(ctx.run_id, step_id, "reserve", args)
        start = None
        if store.resource_by_idempotency_key(ikey) is None:
            windows = ctx.adapter.find_availability(
                ctx.site,
                node_type,
                flavor_id,
                duration_hours,
                start=datetime.fromisoformat(requested) if requested else None,
            )
            if not windows:
                raise CapacityError(
                    f"No availability for {node_type or flavor_name} at {ctx.site}"
                )
            start = windows[0].start

        def _create(with_floating_ip: bool) -> Lease:
            return ctx.adapter.create_lease(
                ctx.site,
                duration_hours=duration_hours,
                start=start,
                node_type=node_type,
                flavor_id=flavor_id,
                with_floating_ip=with_floating_ip,
                run_id=ctx.run_id,
                idempotency_key=ikey,
            )

        try:
            lease = _create(True)
        except CapacityError as exc:
            if not out_of_addresses(exc):
                raise
            store.append_event(
                ctx.run_id,
                "adapter",
                "step_started",
                {
                    "step_id": step_id,
                    "action": "reserve",
                    "note": "no reservable floating IP; retrying without one",
                },
                owner_id=ctx.owner_id,
            )
            lease = _create(False)

        active = ctx.adapter.wait_lease_active(
            ctx.site,
            lease.lease_id,
            timeout_s=_until(lease.start) + ctx.settings.lease_active_s,
            on_poll=lambda: _heartbeat(ctx),
        )

        return {
            "lease_id": active.lease_id,
            "reservation_id": active.reservation_id,
            "reserved_flavor_id": active.reserved_flavor_id,
            "reserved_fip": active.reserved_fips[0] if active.reserved_fips else None,
            "lease_end": active.end,
        }

    except CapacityError as exc:
        _failed(ctx, step_id, "reserve", "plan", exc)
        raise StepError(exc, "plan") from exc
    except StepError:
        raise
    except Exception as exc:
        _failed(ctx, step_id, "reserve", "step", exc)
        raise StepError(exc, "step") from exc


def select_image(ctx: StepContext, args: dict) -> dict:
    """Resolve the image to boot. No match is a "description" failure."""
    step_id: str = args["step_id"]
    try:
        _started(ctx, step_id, "select_image", args)
        approved = ctx.requirements.image
        if approved is not None:
            booted = next(
                (i for i in ctx.adapter.list_images(ctx.site) if i.image_id == approved.image_id),
                None,
            )
            if booted is None:
                gone: Exception = ValueError(
                    f"The approved image {approved.name} ({approved.image_id}) is no longer at "
                    f"{ctx.site}; a new version must choose another, and the user approve it"
                )
                _failed(ctx, step_id, "select_image", "description", gone)
                raise StepError(gone, "description")
            return {"image_id": booted.image_id, "image_name": booted.name}

        os_release: str | None = args.get("os_release") or ctx.requirements.value_of("os_release")
        name: str | None = args.get("image_name")
        cpu_arch: str | None = ctx.requirements.value_of("cpu_arch")

        img = ctx.adapter.find_image(
            ctx.site, os_release=os_release, name=name, cpu_arch=cpu_arch
        )
        if img is None:
            criteria = f"image_name={name!r}" if name else f"os_release={os_release!r}"
            exc: Exception = ValueError(f"No image found matching {criteria} at {ctx.site}")
            _failed(ctx, step_id, "select_image", "description", exc)
            raise StepError(exc, "description")
        return {"image_id": img.image_id, "image_name": img.name}

    except StepError:
        raise
    except Exception as exc:
        _failed(ctx, step_id, "select_image", "step", exc)
        raise StepError(exc, "step") from exc


def ensure_keypair(ctx: StepContext, args: dict) -> dict:
    """Find the run's keypair, creating one from an imported key.

    A missing keypair is a "description" failure.
    """
    step_id: str = args["step_id"]
    try:
        _started(ctx, step_id, "ensure_keypair", args)
        ssh_key_ref: str = args["ssh_key_ref"]

        imported = store.user_key_for_keypair(ctx.owner_id, ssh_key_ref)
        kp = (
            ctx.adapter.import_keypair(ctx.site, ssh_key_ref, imported.public_key)
            if imported is not None
            else ctx.adapter.find_keypair(ctx.site, ssh_key_ref)
        )
        if kp is None:
            exc: Exception = ValueError(
                f"Keypair {ssh_key_ref!r} not found at {ctx.site}"
            )
            _failed(ctx, step_id, "ensure_keypair", "description", exc)
            raise StepError(exc, "description")
        return {"keypair_name": kp.name}

    except StepError:
        raise
    except Exception as exc:
        _failed(ctx, step_id, "ensure_keypair", "step", exc)
        raise StepError(exc, "step") from exc


def _discard_failed_instance(ctx: StepContext, instance_id: str, reason: str) -> None:
    """Delete and release an instance that failed to boot."""
    try:
        with audit.because(reason):
            ctx.adapter.delete_instance(ctx.site, instance_id)
    except Exception:  # noqa: BLE001
        return
    store.release_resource_with_event(ctx.run_id, "instance", instance_id, ctx.site)


def _security_groups_for(ctx: StepContext, step_id: str, args: dict) -> list[str] | None:
    """A run-owned group if the site enforces them, else None."""
    descriptor = ctx.adapter.capabilities().site(ctx.site)
    reserved = reservation_kind_of(ctx.requirements.items)
    kind = descriptor.kind(reserved) if descriptor is not None and reserved else None
    if kind is None or not kind.uses_security_groups:
        return None
    return [
        ctx.adapter.ensure_security_group(
            ctx.site,
            name=resource_name(ctx.run_id),
            run_id=ctx.run_id,
            idempotency_key=derive_key(ctx.run_id, step_id, "security_group", args),
        )
    ]


def _console_log(ctx: StepContext, instance_id: str) -> str:
    """The tail of an instance's console log."""
    try:
        settings = ctx.settings
        return ctx.adapter.get_console_log(
            ctx.site, instance_id, lines=settings.console_lines
        )[-settings.console_chars :]
    except Exception:  # noqa: BLE001
        return "(console log unavailable)"


def boot(ctx: StepContext, args: dict) -> dict:
    """Launch the instance and wait for ACTIVE, attaching the console log on failure."""
    step_id: str = args["step_id"]
    try:
        _started(ctx, step_id, "boot", args)
        reservation_id: str | None = ctx.outputs.get("reservation_id")
        flavor = ctx.outputs.get("reserved_flavor_id")
        if flavor is None and args.get("flavor"):
            found = ctx.adapter.find_flavor(ctx.site, args["flavor"])
            if found is None:
                missing = ValueError(f"No flavor named {args['flavor']!r} at {ctx.site}")
                _failed(ctx, step_id, "boot", "description", missing)
                raise StepError(missing, "description")
            flavor = found.flavor_id
        ikey = derive_key(ctx.run_id, step_id, "boot", args)
        attempts = ctx.settings.boot_attempts
        made = 0
        booted = ""

        def once(number: int) -> dict:
            nonlocal made, booted
            made = number
            inst = ctx.adapter.launch_instance(
                ctx.site,
                image_id=ctx.outputs["image_id"],
                keypair_name=ctx.outputs["keypair_name"],
                name=args.get("name") or resource_name(ctx.run_id),
                reservation_id=reservation_id,
                flavor=flavor,
                security_groups=_security_groups_for(ctx, step_id, args),
                expires_at=ctx.outputs.get("lease_end"),
                run_id=ctx.run_id,
                idempotency_key=ikey,
            )
            booted = inst.instance_id
            try:
                active = ctx.adapter.wait_instance_active(
                    ctx.site,
                    inst.instance_id,
                    timeout_s=ctx.settings.instance_active_s,
                    on_poll=lambda: _heartbeat(ctx),
                )
            except (AdapterTimeoutError, FatalError) as exc:
                _discard_failed_instance(ctx, inst.instance_id, f"it failed to boot: {exc}")
                raise
            return {"instance_id": active.instance_id}

        try:
            return retry.attempt(
                ctx, step_id, "boot", attempts, ctx.settings.boot_retry_s, once
            )
        except (AdapterTimeoutError, FatalError) as exc:
            raise _boot_failed(ctx, step_id, reservation_id, exc, made, booted) from exc

    except StepError:
        raise
    except Exception as exc:
        _failed(ctx, step_id, "boot", "step", exc)
        raise StepError(exc, "step") from exc


def _boot_failed(
    ctx: StepContext,
    step_id: str,
    reservation_id: str | None,
    exc: Exception,
    made: int,
    instance_id: str,
) -> StepError:
    """Error for a boot out of retries."""
    if unusable_host(exc):
        host = ctx.adapter.reserved_host(ctx.site, reservation_id) if reservation_id else None
        unusable: Exception = CapacityError(
            f"Reserved {f'host {host}' if host else 'the reserved host'}, but it is "
            f"not usable after {made} attempt(s): {exc}"
        )
        return StepError(unusable, "plan", retry_reservation=True)

    record_failure(
        ctx.run_id, ctx.owner_id, step_id, "boot", "step", f"{exc} (after {made} attempt(s))",
        console_log=_console_log(ctx, instance_id),
    )
    return StepError(exc, "step")


def assign_ip(ctx: StepContext, args: dict) -> dict:
    """Attach the lease's reserved address, or allocate one if it reserved none."""
    step_id: str = args["step_id"]
    try:
        _started(ctx, step_id, "assign_ip", args)
        instance_id: str = ctx.outputs["instance_id"]
        first = ctx.address_attempt == 1
        address: str | None = ctx.outputs.get("reserved_fip") if first else None

        if not address:
            address = ctx.adapter.allocate_floating_ip(
                ctx.site,
                run_id=ctx.run_id,
                idempotency_key=derive_key(
                    ctx.run_id,
                    step_id,
                    "allocate_ip",
                    {**args, "address_attempt": ctx.address_attempt},
                ),
            )

        ctx.adapter.attach_floating_ip(ctx.site, instance_id, address)
        return {"host": address}

    except CapacityError as exc:
        _failed(ctx, step_id, "assign_ip", "plan", exc)
        raise StepError(exc, "plan") from exc
    except StepError:
        raise
    except Exception as exc:
        _failed(ctx, step_id, "assign_ip", "step", exc)
        raise StepError(exc, "step") from exc


def wait_ssh(ctx: StepContext, args: dict) -> dict:
    """Poll until the host accepts SSH, then write runs.connection."""
    step_id: str = args["step_id"]
    try:
        _started(ctx, step_id, "wait_ssh", args)
        host: str = ctx.outputs["host"]
        user = ctx.settings.ssh_user
        if user is None:
            raise ConfigError(f"set ssh_user for {ctx.site}")
        timeout_s = ctx.settings.ssh_s

        deadline = time.monotonic() + timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = AdapterTimeoutError(
                    f"SSH not available after {timeout_s:.0f}s on {host}",
                    waited_s=timeout_s,
                    last_state="connecting",
                )
                raise StepError(timed_out, "step", retry_address=True)
            try:
                result = ctx.adapter.ssh_exec(
                    ctx.site,
                    host,
                    user,
                    "true",
                    timeout_s=min(15.0, remaining),
                )
                if result.exit_code == 0:
                    break
            except (TransientError, AdapterTimeoutError):
                pass
            _heartbeat(ctx)
            time.sleep(
                min(ctx.settings.ssh_poll_interval_s, max(0.0, deadline - time.monotonic()))
            )

        lease_end: datetime | None = ctx.outputs.get("lease_end")
        connection = Connection(
            host=host,
            user=user,
            port=22,
            expires_at=lease_end.astimezone(timezone.utc).isoformat() if lease_end else None,
        )
        store.update_run(ctx.owner_id, ctx.run_id, connection=connection)
        store.append_event(
            ctx.run_id,
            "runner",
            "connection_ready",
            {
                "host": host,
                "user": user,
                "port": 22,
                "expires_at": connection.expires_at,
            },
            owner_id=ctx.owner_id,
        )
        return {"connection": connection.model_dump()}

    except StepError:
        raise
    except Exception as exc:
        _failed(ctx, step_id, "wait_ssh", "step", exc)
        raise StepError(exc, "step") from exc


def install_unit(run_id: uuid.UUID, step_id: str, args: dict) -> str:
    """The systemd unit name for a run's install."""
    return f"cc-setup-{derive_key(run_id, step_id, 'install_software', args)[:16]}"


def install_software(ctx: StepContext, args: dict) -> dict:
    """Install the description's software, and wait for it to finish."""
    step_id: str = args["step_id"]
    try:
        _started(ctx, step_id, "install_software", args)
        settings = ctx.settings
        host: str = ctx.outputs["host"]
        user = settings.ssh_user
        if user is None:
            raise ConfigError(f"set ssh_user for {ctx.site}")
        software = [(i.key, i.value) for i in ctx.requirements.items if i.key in SETUP_KEYS]
        timeout_s = settings.install_s.total(key for key, _ in software)
        lease_end: datetime | None = ctx.outputs.get("lease_end")
        if lease_end is not None:
            timeout_s = min(timeout_s, _until(lease_end))
        deadline = finish_by()
        job_s = deadline - time.monotonic() if deadline is not None else timeout_s

        def run(command: str, call_s: float) -> tuple[int, str]:
            try:
                result = ctx.adapter.ssh_exec(ctx.site, host, user, command, timeout_s=call_s)
            except (TransientError, AdapterTimeoutError):
                return 1, ""
            return result.exit_code, result.stdout

        install(
            run,
            install_unit(ctx.run_id, step_id, args),
            InstallSettings(
                user=user,
                venv_dir=settings.venv_dir,
                uv_url=settings.uv_url,
                node_dist_url=settings.node_dist_url,
                docker_rpm_repo_url=settings.docker_rpm_repo_url,
                nvidia_toolkit_url=settings.nvidia_toolkit_url,
                install_lock_s=settings.install_lock_s,
            ),
            software,
            timeout_s=min(timeout_s, job_s),
            poll_s=settings.install_poll_interval_s,
            call_s=settings.install_call_s,
            log_lines=settings.install_log_lines,
            on_poll=lambda: _heartbeat(ctx),
        )
        return {}

    except InstallError as exc:
        kind: FailureKind = "description" if exc.kind == "unknown" else "step"
        if exc.kind == "timeout" and job_s < timeout_s:
            exc = InstallError(
                "timeout",
                "the build ran out of time with the install still running; resume the run "
                "to keep waiting for it",
                exc.log,
            )
        record_failure(
            ctx.run_id, ctx.owner_id, step_id, "install_software", kind, str(exc), log=exc.log
        )
        raise StepError(exc, kind) from exc
    except StepError:
        raise
    except Exception as exc:
        _failed(ctx, step_id, "install_software", "step", exc)
        raise StepError(exc, "step") from exc

