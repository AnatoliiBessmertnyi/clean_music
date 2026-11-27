"""Контракт сверки рабочей копии для последующей реализации #18."""

from enum import Enum, auto


class PublicationPhase(Enum):
    COPYING = auto()
    READY = auto()
    PUBLISHING = auto()
    CANCELLED = auto()
    CONFIRMED = auto()


class ObservedFile(Enum):
    ABSENT = auto()
    ORIGINAL = auto()
    VERIFIED_COPY = auto()
    OWNED_INCOMPLETE_COPY = auto()
    UNKNOWN = auto()


class PublicationDecision(Enum):
    STOP = auto()
    CANCEL = auto()
    CANCEL_AND_CLEAN_COPY = auto()
    RESTORE_ORIGINAL = auto()
    CONFIRM_AND_CLEAN_ORIGINAL = auto()
    COMPLETE = auto()


def reconcile_publication(
    phase: PublicationPhase,
    source: ObservedFile,
    work: ObservedFile,
    saved_original: ObservedFile,
) -> PublicationDecision:
    """Выбирает допустимое действие по доказанным наблюдениям, без ввода-вывода."""
    absent = ObservedFile.ABSENT
    original = ObservedFile.ORIGINAL
    verified = ObservedFile.VERIFIED_COPY
    if source is original and saved_original is absent:
        if phase is PublicationPhase.COPYING:
            if work is absent:
                return PublicationDecision.CANCEL
            if work is ObservedFile.OWNED_INCOMPLETE_COPY:
                return PublicationDecision.CANCEL_AND_CLEAN_COPY
        if (
            phase in (PublicationPhase.READY, PublicationPhase.PUBLISHING)
            and work is verified
        ):
            return PublicationDecision.CANCEL_AND_CLEAN_COPY
        if phase is PublicationPhase.CANCELLED:
            if work is absent:
                return PublicationDecision.COMPLETE
            if work in (verified, ObservedFile.OWNED_INCOMPLETE_COPY):
                return PublicationDecision.CANCEL_AND_CLEAN_COPY
    if (
        phase is PublicationPhase.PUBLISHING
        and source is absent
        and work is verified
        and saved_original is original
    ):
        return PublicationDecision.RESTORE_ORIGINAL
    if (
        phase in (PublicationPhase.PUBLISHING, PublicationPhase.CONFIRMED)
        and source is verified
        and work is absent
    ):
        if saved_original is original:
            return PublicationDecision.CONFIRM_AND_CLEAN_ORIGINAL
        if phase is PublicationPhase.CONFIRMED and saved_original is absent:
            return PublicationDecision.COMPLETE
    return PublicationDecision.STOP
