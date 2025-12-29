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
#   --database=auth   Target auth database only
#   --database=cases  Target cases database only
#   --sql             Generate SQL without executing (offline mode)
#   --verbose         Enable verbose output
#
# Examples:
#   ./scripts/db_migrate.sh upgrade                    # Apply all migrations
#   ./scripts/db_migrate.sh upgrade --database=cases   # Apply to cases DB only
#   ./scripts/db_migrate.sh downgrade                  # Rollback one migration
#   ./scripts/db_migrate.sh create add_user_roles      # Create new migration
#   ./scripts/db_migrate.sh status                     # Check current status
#
# Environment Variables:
#   DATABASE_URL     - Primary database URL
#   AUTH_DB_URL      - Auth database URL
#   CASES_DB_URL     - Cases database URL
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
DATABASE=""
SQL_MODE=""
VERBOSE=""
REVISION=""

while [[ $# -gt 0 ]]; do
    case $1 in
        upgrade|downgrade|status|history|create|heads|check|stamp)
            COMMAND="$1"
            shift
            ;;
        --database=*)
            DATABASE="${1#*=}"
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

# Build database option
DB_OPTION=""
if [[ -n "$DATABASE" ]]; then
    DB_OPTION="-x database=$DATABASE"
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
    echo "  --database=auth   Target auth database only"
    echo "  --database=cases  Target cases database only"
    echo "  --sql             Generate SQL without executing"
    echo "  --verbose, -v     Enable verbose output"
    echo ""
    echo "Examples:"
    echo "  $0 upgrade                    # Apply all migrations"
    echo "  $0 upgrade --database=cases   # Apply to cases DB only"
    echo "  $0 downgrade                  # Rollback one migration"
    echo "  $0 create add_user_roles      # Create new migration"
    echo "  $0 status                     # Check current status"
}

# Execute command
case $COMMAND in
    upgrade)
        echo -e "${GREEN}Applying migrations...${NC}"
        if [[ -n "$DATABASE" ]]; then
            echo -e "${BLUE}Target database: $DATABASE${NC}"
        fi
        alembic $DB_OPTION $SQL_MODE $VERBOSE upgrade head
        echo -e "${GREEN}✓ Migrations applied successfully${NC}"
        ;;

    downgrade)
        echo -e "${YELLOW}Rolling back one migration...${NC}"
        if [[ -n "$DATABASE" ]]; then
            echo -e "${BLUE}Target database: $DATABASE${NC}"
        fi
        alembic $DB_OPTION $SQL_MODE $VERBOSE downgrade -1
        echo -e "${GREEN}✓ Rollback completed successfully${NC}"
        ;;

    status)
        echo -e "${BLUE}Current migration status:${NC}"
        if [[ -n "$DATABASE" ]]; then
            echo -e "${BLUE}Target database: $DATABASE${NC}"
        fi
        alembic $DB_OPTION current $VERBOSE
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
        if [[ -n "$DATABASE" ]]; then
            echo -e "${BLUE}Target database: $DATABASE${NC}"
        fi
        alembic $DB_OPTION stamp $REVISION
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
