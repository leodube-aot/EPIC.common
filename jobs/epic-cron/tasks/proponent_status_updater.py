"""Proponent Status Updater - Updates proponent eligibility status.

Business Logic:
    A proponent is ELIGIBLE if either:
    1. They have at least one project with approved conditions, OR
    2. They have at least one valid work where:
       - The work's current phase has enable_submit = True
       - The work state is NOT in [COMPLETED, TERMINATED, CLOSED, WITHDRAWN]
       - The work is active and not deleted

    Note: Only INELIGIBLE proponents, or proponents without a status,
          are changed to ELIGIBLE. 
    
    A proponent is INELIGIBLE if:
    - They do NOT meet the ELIGIBLE criteria above, AND
    - They currently have no status set (status is None)
    
    Note: Proponents with existing status values are not changed to INELIGIBLE.
"""
from flask import current_app
from sqlalchemy import and_, or_

from submit_api.models.proponent import Proponent as SubmitProponentModel
from submit_api.models.project import Project as SubmitProjectModel
from submit_api.enums.proponent_status import ProponentStatus
from epic_cron.services.approved_condition_service import ApprovedConditionService
from epic_cron.models.external.track_work import TrackWork
from epic_cron.models.external.track_phase import TrackPhase
from epic_cron.models.external.work_state import WorkState


class ProponentStatusUpdater:
    """Updates proponent eligibility status based on approved conditions and valid works."""

    @classmethod
    def update(cls, db, _=None):
        """Main entry point for updating proponent eligibility status."""
        current_app.logger.info("Running ProponentStatusUpdater...")
        
        try:
            with db.session() as session:
                # Find all proponents who should be ELIGIBLE
                eligible_proponent_ids = cls._find_eligible_proponents(session)
                
                # Update proponents to ELIGIBLE status
                if eligible_proponent_ids:
                    cls._update_proponent_status(session, eligible_proponent_ids, ProponentStatus.ELIGIBLE)
                else:
                    current_app.logger.info("No eligible proponents found.")
                
                # Find and update proponents who should be INELIGIBLE
                ineligible_proponent_ids = cls._find_ineligible_proponents(session, eligible_proponent_ids)
                if ineligible_proponent_ids:
                    cls._update_proponent_status(session, ineligible_proponent_ids, ProponentStatus.INELIGIBLE)
                else:
                    current_app.logger.info("No ineligible proponents found.")
                
                current_app.logger.info(
                    f"ProponentStatusUpdater completed. "
                    f"ELIGIBLE: {len(eligible_proponent_ids)}, INELIGIBLE: {len(ineligible_proponent_ids)}"
                )
        except Exception as e:
            current_app.logger.error(f"Error in ProponentStatusUpdater: {e}", exc_info=True)
            raise

    @classmethod
    def _find_eligible_proponents(cls, session):
        """Find all proponents who meet eligibility criteria.
        
        Returns:
            set: Set of proponent IDs that are eligible
        """
        # Criterion 1: Proponents with approved conditions
        proponents_with_conditions = cls._find_proponents_with_approved_conditions(session)
        current_app.logger.info(
            f"Found {len(proponents_with_conditions)} proponents with approved conditions"
        )
        
        # Criterion 2: Proponents with valid works
        proponents_with_valid_works = cls._find_proponents_with_valid_works(session)
        current_app.logger.info(
            f"Found {len(proponents_with_valid_works)} proponents with valid works"
        )
        
        # Combine both sets (OR logic)
        eligible_proponents = proponents_with_conditions.union(proponents_with_valid_works)
        current_app.logger.info(
            f"Total eligible proponents: {len(eligible_proponents)} "
            f"(conditions: {len(proponents_with_conditions)}, valid works: {len(proponents_with_valid_works)})"
        )
        
        return eligible_proponents

    @classmethod
    def _find_proponents_with_approved_conditions(cls, session):
        """Find proponents who have at least one project with approved conditions.
        
        Returns:
            set: Set of proponent IDs with approved conditions
        """
        # Sync approved conditions first
        proponent_ids_from_sync = ApprovedConditionService.sync_approved_conditions(session)
        
        if proponent_ids_from_sync:
            return set(proponent_ids_from_sync)
        
        # Fallback: Query projects with has_approved_condition flag
        projects_with_conditions = session.query(SubmitProjectModel.proponent_id).filter(
            SubmitProjectModel.has_approved_condition == True
        ).distinct().all()
        
        return {p.proponent_id for p in projects_with_conditions}

    @classmethod
    def _find_proponents_with_valid_works(cls, session):
        """Find proponents who have at least one valid work.
        
        A valid work is one where:
        - The work's current phase has enable_submit = True
        - The work state is NOT in excluded states (COMPLETED, TERMINATED, CLOSED, WITHDRAWN)
        - The work is active and not deleted
        
        Returns:
            set: Set of proponent IDs with valid works
        """
        excluded_states = WorkState.excluded_states()
        
        # Query: Join track_works -> track_phases -> projects -> proponents
        # Filter for valid works with enable_submit phases
        valid_work_proponents = (
            session.query(SubmitProjectModel.proponent_id)
            .join(TrackWork, SubmitProjectModel.id == TrackWork.project_id)
            .join(TrackPhase, TrackWork.current_phase_id == TrackPhase.id)
            .filter(
                and_(
                    # Phase must have enable_submit = True
                    TrackPhase.enable_submit == True,
                    # Work must be active and not deleted
                    TrackWork.is_active == True,
                    TrackWork.is_deleted == False,
                    # Work state must NOT be in excluded states
                    TrackWork.work_state.notin_(excluded_states)
                )
            )
            .distinct()
            .all()
        )
        
        return {p.proponent_id for p in valid_work_proponents}

    @classmethod
    def _find_ineligible_proponents(cls, session, eligible_proponent_ids):
        """Find proponents who should be marked as INELIGIBLE.
        
        A proponent is INELIGIBLE if:
        - They are NOT in the eligible list (no approved conditions and no valid works)
        - They currently have no status set (status is None)
        
        Args:
            session: Database session
            eligible_proponent_ids: Set of proponent IDs that are eligible
        
        Returns:
            set: Set of proponent IDs that should be marked INELIGIBLE
        """
        # Query all proponents with no status who are not in the eligible list
        ineligible_proponents = (
            session.query(SubmitProponentModel.id)
            .filter(
                and_(
                    SubmitProponentModel.status.is_(None),  # No status set
                    SubmitProponentModel.id.notin_(eligible_proponent_ids) if eligible_proponent_ids else True
                )
            )
            .all()
        )
        
        ineligible_ids = {p.id for p in ineligible_proponents}
        current_app.logger.info(
            f"Found {len(ineligible_ids)} proponents with no status who are not eligible"
        )
        
        return ineligible_ids

    @classmethod
    def _update_proponent_status(cls, session, proponent_ids, status):
        """Update the status of proponents.
        
        Args:
            session: Database session
            proponent_ids: Set or list of proponent IDs to update
            status: ProponentStatus enum value to set
        """
        if not proponent_ids:
            return
        
        proponents = session.query(SubmitProponentModel).filter(
            and_(
                SubmitProponentModel.id.in_(proponent_ids),
                or_(
                    SubmitProponentModel.status.is_(None),
                    SubmitProponentModel.status == ProponentStatus.INELIGIBLE
                )
            )
        ).all()
        
        updated_count = 0
        for proponent in proponents:
            if proponent.status != status:
                proponent.status = status
                updated_count += 1
        
        session.commit()
        current_app.logger.info(
            f"Updated {updated_count} proponents to {status.value} status "
            f"(out of {len(proponent_ids)} eligible)"
        )
