#!/bin/bash
#
# FaultMaven Database Migration Helper Script
#
# This script provides convenient wrappers for common Alembic operations.
#
# Usage:
#   ./scripts/db_migrate.sh <command> [options]
#
# Commands:
#   upgrade           Apply all pending migrations
#   downgrade         Rollback one migration
#   status            Show current migration status
#   history           Show migration history
#   create <message>  Create a new migration
#   heads             Show current heads
#   check             Check migration consistency
#   stamp <revision>  Stamp database with revision (without running migrations)
#
# Options:
#   --sql             upgrade only: print the SQL instead of executing it
#                     (offline mode)
#   --verbose, -v     status, heads: verbose output (history is always verbose)
#
# Examples:
#   ./scripts/db_migrate.sh upgrade                    # Apply all migrations
#   ./scripts/db_migrate.sh upgrade --sql              # Print the SQL only
#   ./scripts/db_migrate.sh downgrade                  # Rollback one migration
#   ./scripts/db_migrate.sh create add_user_roles      # Create new migration
#   ./scripts/db_migrate.sh status                     # Check current status
#
# Environment Variables:
#   DATABASE_URL     - The one database FaultMaven uses (see alembic/env.py)
#

set -e

# Color codes for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# Change to project root
cd "$PROJECT_ROOT"

# Parse arguments
COMMAND=""
MESSAGE=""
SQL_MODE=""
VERBOSE=""
REVISION=""

while [[ $# -gt 0 ]]; do
    case $1 in
        upgrade|downgrade|status|history|create|heads|check|stamp)
            COMMAND="$1"
            shift
            ;;
        --sql)
            SQL_MODE="--sql"
            shift
            ;;
        --verbose|-v)
            VERBOSE="-v"
            shift
            ;;
        -*)
            echo -e "${RED}Error: unknown option: $1${NC}" >&2
            echo "Run $0 with no arguments for usage." >&2
            exit 2
            ;;
        *)
            if [[ -z "$MESSAGE" && "$COMMAND" == "create" ]]; then
                MESSAGE="$1"
            elif [[ -z "$REVISION" && "$COMMAND" == "stamp" ]]; then
                REVISION="$1"
            fi
            shift
            ;;
    esac
done

# Each option belongs to the alembic subcommands that accept it, and alembic
# only takes a subcommand's options AFTER the subcommand. Refuse an option the
# command cannot honour rather than drop it silently.
if [[ -n "$SQL_MODE" && "$COMMAND" != "upgrade" ]]; then
    echo -e "${RED}Error: --sql applies to 'upgrade' only${NC}" >&2
    exit 2
fi
if [[ -n "$VERBOSE" && "$COMMAND" != "status" && "$COMMAND" != "heads" ]]; then
    echo -e "${RED}Error: --verbose applies to 'status' and 'heads' only${NC}" >&2
    exit 2
fi

# Help function
show_help() {
    echo -e "${BLUE}FaultMaven Database Migration Helper${NC}"
    echo ""
    echo "Usage: $0 <command> [options]"
    echo ""
    echo "Commands:"
    echo "  upgrade           Apply all pending migrations"
    echo "  downgrade         Rollback one migration"
    echo "  status            Show current migration status"
    echo "  history           Show migration history"
    echo "  create <message>  Create a new migration"
    echo "  heads             Show current heads"
    echo "  check             Check migration consistency"
    echo "  stamp <revision>  Stamp database with revision"
    echo ""
    echo "Options:"
    echo "  --sql             upgrade only: print the SQL instead of executing it"
    echo "  --verbose, -v     status, heads: verbose output"
    echo ""
    echo "Examples:"
    echo "  $0 upgrade                    # Apply all migrations"
    echo "  $0 upgrade --sql              # Print the SQL only"
    echo "  $0 downgrade                  # Rollback one migration"
    echo "  $0 create add_user_roles      # Create new migration"
    echo "  $0 status                     # Check current status"
}

# Execute command
case $COMMAND in
    upgrade)
        if [[ -n "$SQL_MODE" ]]; then
            alembic upgrade head --sql
        else
            echo -e "${GREEN}Applying migrations...${NC}"
            alembic upgrade head
            echo -e "${GREEN}✓ Migrations applied successfully${NC}"
        fi
        ;;

    downgrade)
        echo -e "${YELLOW}Rolling back one migration...${NC}"
        alembic downgrade -1
        echo -e "${GREEN}✓ Rollback completed successfully${NC}"
        ;;

    status)
        echo -e "${BLUE}Current migration status:${NC}"
        alembic current $VERBOSE
        ;;

    history)
        echo -e "${BLUE}Migration history:${NC}"
        alembic history --verbose
        ;;

    create)
        if [[ -z "$MESSAGE" ]]; then
            echo -e "${RED}Error: Migration message required${NC}"
            echo "Usage: $0 create <message>"
            exit 1
        fi
        echo -e "${GREEN}Creating new migration: $MESSAGE${NC}"
        alembic revision -m "$MESSAGE"
        echo -e "${GREEN}✓ Migration created successfully${NC}"
        echo ""
        echo -e "${YELLOW}Note: Edit the migration file to add your schema changes.${NC}"
        ;;

    heads)
        echo -e "${BLUE}Current heads:${NC}"
        alembic heads $VERBOSE
        ;;

    check)
        echo -e "${BLUE}Checking migration consistency...${NC}"
        alembic check
        echo -e "${GREEN}✓ Migrations are consistent${NC}"
        ;;

    stamp)
        if [[ -z "$REVISION" ]]; then
            echo -e "${RED}Error: Revision required${NC}"
            echo "Usage: $0 stamp <revision>"
            echo "Use 'head' to stamp with latest revision"
            exit 1
        fi
        echo -e "${YELLOW}Stamping database with revision: $REVISION${NC}"
        alembic stamp "$REVISION"
        echo -e "${GREEN}✓ Database stamped successfully${NC}"
        ;;

    "")
        show_help
        ;;

    *)
        echo -e "${RED}Unknown command: $COMMAND${NC}"
        echo ""
        show_help
        exit 1
        ;;
esac
