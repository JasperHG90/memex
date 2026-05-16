# Project Zeta — Q3 Incident Report

**Date:** September 28, 2025
**Severity:** P1
**Duration:** 4 hours 23 minutes

## Incident Summary

Project Zeta experienced a major outage on September 28, 2025 caused by a cascading
database connection pool exhaustion. Approximately 15% of users were unable to access
the platform for over 4 hours. The root cause was an unbounded connection pool in the
ORM layer that failed under unexpected traffic patterns.

## Impact

The incident resulted in an estimated $2.3M in lost revenue and 340 customer complaints.
The SLA breach triggered penalty clauses in 12 enterprise contracts. Recovery required
manual intervention by the database team to restart the connection pool.
