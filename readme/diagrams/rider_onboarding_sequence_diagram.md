# SmartFoodOps — Rider Onboarding Sequence Diagram

Joining the delivery fleet and going on shift — the one diagram for a rider becoming
dispatchable. Verified against `services/rider/apis/profile.py`. Assumes the rider already
has an account — see
[user_registration_sequence_diagram.md](user_registration_sequence_diagram.md). Once
available and located, [order_flow_sequence_diagram.md](order_flow_sequence_diagram.md)
covers dispatch, pickup and delivery.

```mermaid
sequenceDiagram
    autonumber
    actor Rider
    participant Gateway
    participant RiderSvc as Rider Service
    participant UserSvc as User Service
    participant RiderDB as sfo_rider_core
    participant RiderGeo as Redis (riders:geo, db 2)

    rect rgb(235, 245, 255)
        Note over Rider, RiderDB: Join the fleet
        Rider->>Gateway: POST /riders
        Gateway->>RiderSvc: forward
        RiderSvc->>UserSvc: verify caller holds rider role
        break account missing, or demoted since the token was issued
            UserSvc-->>RiderSvc: not found / wrong role
            RiderSvc-->>Rider: 404 Not Found / 403 Forbidden
        end
        UserSvc-->>RiderSvc: verified
        RiderSvc->>RiderDB: insert rider row
        break already enrolled, or vehicle number taken
            RiderDB-->>RiderSvc: unique constraint violation
            RiderSvc-->>Rider: 409 Conflict
        end
        RiderSvc-->>Rider: 201 Created
    end

    rect rgb(240, 240, 240)
        Note over Rider, RiderGeo: Go on shift
        Rider->>Gateway: PATCH /riders/me/location
        Gateway->>RiderSvc: forward
        RiderSvc->>RiderDB: confirm the rider exists (404 check only)
        RiderSvc->>RiderGeo: GEOADD riders:geo (D49 -- no longer a Postgres column)
        break Redis unreachable
            RiderGeo-->>RiderSvc: connection error
            RiderSvc-->>Rider: 503 Service Unavailable
            Note right of RiderSvc: no Postgres fallback left for location -- fails loud, not soft
        end
        RiderSvc-->>Rider: 200 OK
        Rider->>Gateway: PATCH /riders/me/availability
        Gateway->>RiderSvc: forward
        RiderSvc->>RiderDB: set is_available = true
        break rider is currently carrying an order
            RiderDB-->>RiderSvc: no row updated -- mid-delivery
            RiderSvc-->>Rider: 409 Conflict
            Note right of RiderSvc: must finish or lose the delivery before going off shift
        end
        RiderSvc-->>Rider: 200 OK
    end
```
