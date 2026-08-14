# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

The primary user is a business owner or team leader who manages client-facing work conducted in Telegram. They need to see where the team has left a valuable conversation, promise, task, complaint, payment question, or follow-up unresolved without manually reading every chat.

Other permitted users are tenant managers and employees with server-enforced access limited to their role and scope. The product is multi-tenant; no user may receive another tenant's data.

## Product Purpose

Ventrix is a mobile-first Telegram Mini App that turns relevant working conversations into an operational control loop. It identifies real unfinished actions, links each conclusion to evidence, assigns responsibility, tracks deadlines, observes subsequent messages, and verifies whether the underlying issue was actually resolved.

Success means that an owner can open Ventrix and immediately understand what genuinely requires management attention, who is responsible, what should happen next, and whether the situation was fixed. Precision is more important than recall: weak or ambiguous situations should not create noisy alerts.

## Positioning

Ventrix is not a CRM, shared inbox, message counter, generic AI dashboard, or employee-surveillance product. The team continues working in Telegram while Ventrix provides a tenant-isolated evidence-to-remediation layer over those conversations.

Its primary unit is a managed business problem rather than an individual message or opaque AI score. A finding is valuable only when it has source evidence, business meaning, an owner, a lifecycle, and a verifiable outcome.

## Operating Context

- The Mini App is used primarily inside Telegram on a smartphone; direct-browser access is a fallback rather than the main authorization flow.
- A tenant connects authorized working Telegram accounts and selects the allowed personal dialogs or groups for analysis.
- Telegram `initData` is validated by the backend and resolves tenant, user, role, and permissions server-side.
- Owners and managers review problems, commitments, reports, employees, Telegram connections, groups, and understandable company metrics.
- Employees may receive and manage only the problems, commitments, and data allowed by their membership and permissions.
- Client bots deliver concise alerts, summaries, assignments, reminders, reports, and secure links into the relevant Mini App context.

## Capabilities and Constraints

- Preserve the existing business logic, backend API boundary, Telegram authorization, tenant isolation, worker pipeline, bots, scheduler, and persistent problem lifecycle during interface redesigns.
- Analyse message direction correctly: `EMPLOYEE/outgoing` is the monitored working account and `CLIENT/incoming` is the external person.
- Create a problem only for a concrete, valuable, unfinished action supported by conversation context and evidence.
- Support assignment, deadlines, status transitions, false-positive feedback, reopen, remediation verification, notifications, commitments, and consolidated reports.
- Treat codes, 2FA passwords, bot tokens, Telegram sessions, private messages, and tenant data as sensitive. Confirmation codes and 2FA passwords are not retained.
- Show business users understandable business metrics only. Internal token counts, AI limits, prompt mechanics, debug data, queue internals, and infrastructure details do not belong in the tenant-facing interface.
- The product remains a single-host modular monolith for the current pilot and must not require a backend rewrite to support interface work.

## Brand Commitments

- Product name: Ventrix.
- Primary product language: Russian, with clear business terminology and no unexplained technical jargon.
- The interface must feel calm, premium, and B2B-focused without the visual noise typical of generic AI SaaS products.
- Both dark and light themes are required.
- Mobile layouts must respect real Telegram safe areas, dynamic viewport behavior, and Telegram theme parameters.
- Motion is purposeful and moderate: navigation transitions, charts, state changes, loading, and onboarding may animate, but the operational Mini App must not behave like a marketing landing page.

## Evidence on Hand

- Working Mini App implementation: `app/mini-app/`.
- Current product and architecture decisions: `docs/product-and-architecture.md`.
- Telegram inline UI contract: `docs/telegram-inline-ui.md`.
- Backend, Telegram bots, analysis pipeline, migrations, and automated tests are present in this repository.
- Existing screenshots and pilot feedback document real interface and workflow problems, but there are no approved testimonials, customer logos, external benchmark claims, or validated revenue claims that future design work may fabricate.

## Product Principles

1. Show only situations that deserve a manager's action; prefer no alert over a weak alert.
2. Make every conclusion understandable, attributable, tenant-safe, and linked to source evidence.
3. Keep the team working in Telegram while Ventrix coordinates ownership and verifies outcomes.
4. Present business meaning rather than AI or infrastructure mechanics.
5. Let interface and visual design evolve independently of authentication, backend contracts, and core business logic.

## Accessibility & Inclusion

- Core workflows must remain readable and operable across Telegram's supported mobile viewports, text scaling, safe areas, and both light and dark themes.
- Status must not be communicated by color alone.
- Controls, charts, loading states, and motion must remain understandable for users who reduce motion or use assistive technology.
