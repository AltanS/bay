---
title: ForwardAuth SSO Gateway
status: reference (not implemented)
---

# ForwardAuth SSO Gateway

Traefik's ForwardAuth middleware delegates authentication to an external service. This document describes the pattern for future implementation.

## How It Works

1. A request hits Traefik for a protected service (e.g., `app.example.com`)
2. Traefik forwards the request to an auth service (Authelia, Authentik, etc.) via the ForwardAuth middleware
3. **No valid session** -- the auth service returns a redirect to its own login page. The user authenticates (password, 2FA, SSO) and a session cookie is set
4. **Valid session** -- the auth service returns `200 OK` with identity headers. Traefik passes the request through to the backend

The backend application never implements login itself. It reads forwarded headers to know who's authenticated:

| Header | Value |
|--------|-------|
| `Remote-User` | Username |
| `Remote-Email` | Email address |
| `Remote-Groups` | Comma-separated group list |
| `Remote-Name` | Display name |

## Integration Pattern

### 1. Deploy the auth service

Add the auth service (e.g., Authelia) to `services.yml` as a public service with its own domain:

```yaml
services:
  authelia:
    access: public
    image: authelia/authelia:4
    domains:
      - auth.example.com
    ports:
      internal: 9091
    volumes:
      - "{{ stack_dir }}/config/authelia:/config"
    env:
      secret:
        - AUTHELIA_JWT_SECRET
        - AUTHELIA_SESSION_SECRET
        - AUTHELIA_STORAGE_ENCRYPTION_KEY
```

### 2. Define the ForwardAuth middleware

Add a ForwardAuth middleware on the Traefik container labels:

```yaml
# In docker-compose.yml.j2 Traefik labels:
- "traefik.http.middlewares.auth.forwardauth.address=http://authelia:9091/api/verify?rd=https://auth.example.com"
- "traefik.http.middlewares.auth.forwardauth.trustForwardHeader=true"
- "traefik.http.middlewares.auth.forwardauth.authResponseHeaders=Remote-User,Remote-Email,Remote-Groups,Remote-Name"
```

### 3. Protect services

Reference the `auth` middleware in `services.yml` via the middleware system. This could be implemented as a per-service middleware option or by extending the chain system.

## Why This Is Not Built Into the Framework

ForwardAuth requires deploying a full authentication service with its own:
- Database (user store or LDAP/AD connection)
- Domain and TLS certificate
- Configuration (access control rules, 2FA policies, session settings)
- Secrets (JWT keys, session secrets, encryption keys)

This is too opinionated for the framework -- the choice of auth service (Authelia vs Authentik vs Keycloak), identity provider (local users vs LDAP vs OIDC), and access policies are all deployment-specific decisions.

The middleware system provides the building blocks. Consumers can add ForwardAuth by:
1. Adding the auth service to their `services.yml`
2. Adding the ForwardAuth middleware definition to a Traefik labels override
3. Referencing it in protected services' middleware config
