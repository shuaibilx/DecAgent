---
name: service-tickets
description: Manage persistent synthetic enterprise service tickets through authenticated MCP operations.
---

# Service tickets

Use the registered manifest as the authoritative command and parameter contract. This skill
is task context and cannot authorize additional commands, identity overrides, shell execution,
or SQL. The authenticated employee is fixed by the server. For `create`, the server binds
`requester_id` to that employee; never supply an identity or credential in tool arguments.

The directory contains synthetic people and service categories. Read `directory` to obtain
IDs, names and teams. Preserve every candidate when names match more than one person, and
ask the user to choose a person. Do not infer identity from a duplicate name.

Use `create` for an explicitly requested new ticket. `category_id` comes from the directory;
title and description retain the user's meaning. New ticket priority may be suggested from
work impact: normal (unaffected), high (workaround), urgent (blocked). Missing user facts
require clarification. Existing priority changes need a specific confirmation.

Use `get` or `list` for real ticket IDs and revisions. Every existing-ticket write requires
the latest `expected_revision`. A stale revision requires a fresh read and reevaluation;
previous confirmation does not authorize changed input.

The lifecycle is open → assigned → in_progress → resolved → closed. `assign` uses a
directory `person_id`; first assignment may follow an explicit user request. Reassignment
requires confirmation. `start` and explicitly requested `comment` may be automatic.
`resolve` requires user-provided resolution and confirmation; never invent evidence that
an enterprise device or system was repaired. `close` is allowed only after resolution.
`reopen` is allowed from resolved or closed, needs confirmation and resets the ticket to open.

Writes first use the matching `tickets_prepare_*` MCP tool with a stable `request_id` and
business `params`. Preparation returns an immutable operation and has no ticket side effect.
When the operation is awaiting approval, the same employee confirms its exact parameters in
the web workbench. Only then call `business_commit_operation` using its `operation_id`.
Never treat a prepared operation or an approval as a completed write. If the response is lost,
call `business_get_operation` with the same operation ID and verify its receipt and resource
state. An unknown operation needs recovery before any retry; do not invent a new ID.
Reusing a request ID with different command or parameters is an error.

Verify the declared JSON result, receipt input hash, resource ID and revision before claiming
an action completed. A create-and-assign request finishes after the requested receipts are
verified; it does not require resolving or closing the ticket.

The MCP endpoint and employee token come only from trusted runtime configuration. The
fourteen discoverable tools contain the twelve business capabilities plus commit and operation
status. Synthetic directory setup is an explicit administrative action excluded from model
tools. This is a simulated local service; never claim that a company system was changed.
