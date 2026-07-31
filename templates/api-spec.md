# API Specification — <Resource Name>

| Field | Value |
| --- | --- |
| **Status** | Draft \| Review \| Approved \| Implemented |
| **Owner** | <name> |
| **API version** | `v1` |
| **Base path** | `/api/v1/<resource>` |
| **Related spec** | `specs/<feature>.md` |
| **Last updated** | YYYY-MM-DD |

---

## 1. Overview

What this resource represents and which feature it belongs to.

## 2. Conventions

Inherits the platform conventions in `docs/03-backend/api.md`. Note any deviation here
with a justification — deviations require an ADR.

## 3. Endpoints

### `<METHOD> /api/v1/<path>`

**Purpose:** <one sentence>

**Authentication:** Required \| Optional \| Public
**Authorization:** <role or ownership rule>
**Idempotent:** Yes \| No
**Rate limit:** <n> requests / <window> per <subject>

#### Path Parameters

| Name | Type | Required | Description |
| --- | --- | --- | --- |
| | | | |

#### Query Parameters

| Name | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| | | | | |

#### Request Body

| Field | Type | Required | Constraints | Description |
| --- | --- | --- | --- | --- |
| | | | | |

```json
{
  "example": "request"
}
```

#### Success Response

**Status:** `200 OK`

| Field | Type | Nullable | Description |
| --- | --- | --- | --- |
| | | | |

```json
{
  "example": "response"
}
```

#### Error Responses

| Status | Code | Condition | Notes |
| --- | --- | --- | --- |
| `400` | `VALIDATION_ERROR` | Request failed validation | Includes field details |
| `401` | `UNAUTHENTICATED` | Missing or invalid credentials | |
| `403` | `FORBIDDEN` | Authenticated but not permitted | |
| `404` | `NOT_FOUND` | Resource does not exist or is not visible | |
| `409` | `CONFLICT` | State conflict | |
| `429` | `RATE_LIMITED` | Rate limit exceeded | Includes `Retry-After` |

#### Side Effects

Events emitted, caches invalidated, notifications sent.

---

## 4. Data Model Reference

Link the entities involved and where their canonical definition lives.

## 5. Pagination

Strategy, parameters, and response envelope for list endpoints.

## 6. Caching

Cacheability, TTL, cache keys, and invalidation triggers.
See `docs/01-architecture/caching.md`.

## 7. Versioning & Deprecation

How breaking changes are introduced and how long the previous version is supported.

## 8. Test Cases

| ID | Scenario | Expected |
| --- | --- | --- |
| T-1 | | |

## 9. TODO

- [ ] Complete every endpoint section
- [ ] Confirm error codes match the platform error catalogue
- [ ] Review against `docs/01-architecture/security.md`
