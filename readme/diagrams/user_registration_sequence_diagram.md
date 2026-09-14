# SmartFoodOps — User Registration & Session Sequence Diagram

Register, log in, refresh, and log out — the one diagram for identity, common to every
role (`customer`, `restaurant_admin`, `rider`, `system_admin`). Verified against
`services/user/apis/users.py` and `services/user/apis/sessions.py`.

```mermaid
sequenceDiagram
    autonumber
    actor Client as Customer / Owner / Rider
    participant Gateway
    participant UserSvc as User Service
    participant UserDB as sfo_user_core
    participant Redis as Redis (refresh tokens, db 1)

    rect rgb(235, 245, 255)
        Note over Client, UserDB: Register
        Client->>Gateway: POST /users/register
        Gateway->>UserSvc: forward
        UserSvc->>UserDB: hash password, insert user row
        break email or phone already registered
            UserDB-->>UserSvc: unique constraint violation
            UserSvc-->>Client: 409 Conflict
        end
        UserSvc-->>Client: 201 Created
    end

    rect rgb(240, 240, 240)
        Note over Client, Redis: Log in
        Client->>Gateway: POST /users/login
        Gateway->>UserSvc: forward
        UserSvc->>UserDB: verify credentials
        break wrong email or password
            UserSvc-->>Client: 401 Unauthorized
        end
        UserSvc->>Redis: store refresh token
        UserSvc-->>Client: access + refresh token
    end

    rect rgb(255, 245, 230)
        Note over Client, Redis: Refresh
        Client->>Gateway: POST /users/refresh
        Gateway->>UserSvc: forward
        UserSvc->>Redis: consume & rotate refresh token
        break token invalid, expired, or already used
            UserSvc-->>Client: 401 Unauthorized
        end
        UserSvc->>UserDB: re-read current role
        UserSvc-->>Client: new access + refresh token
    end

    rect rgb(255, 230, 230)
        Note over Client, Redis: Log out
        Client->>Gateway: POST /users/logout
        Gateway->>UserSvc: forward
        UserSvc->>Redis: revoke refresh token
        UserSvc-->>Client: 200 OK
    end
```
