import pytest

from publication_recovery import ObservedFile as File
from publication_recovery import PublicationDecision as Decision
from publication_recovery import PublicationPhase as Phase
from publication_recovery import reconcile_publication


@pytest.mark.parametrize(
    ('phase', 'source', 'work', 'saved', 'expected'),
    [
        (Phase.COPYING, File.ORIGINAL, File.ABSENT, File.ABSENT, Decision.CANCEL),
        (
            Phase.COPYING,
            File.ORIGINAL,
            File.OWNED_INCOMPLETE_COPY,
            File.ABSENT,
            Decision.CANCEL_AND_CLEAN_COPY,
        ),
        (Phase.COPYING, File.ORIGINAL, File.UNKNOWN, File.ABSENT, Decision.STOP),
        (
            Phase.READY,
            File.ORIGINAL,
            File.VERIFIED_COPY,
            File.ABSENT,
            Decision.CANCEL_AND_CLEAN_COPY,
        ),
        (
            Phase.PUBLISHING,
            File.ABSENT,
            File.VERIFIED_COPY,
            File.ORIGINAL,
            Decision.RESTORE_ORIGINAL,
        ),
        (
            Phase.PUBLISHING,
            File.UNKNOWN,
            File.VERIFIED_COPY,
            File.ORIGINAL,
            Decision.STOP,
        ),
        (Phase.PUBLISHING, File.ABSENT, File.UNKNOWN, File.ORIGINAL, Decision.STOP),
        (
            Phase.PUBLISHING,
            File.VERIFIED_COPY,
            File.ABSENT,
            File.ORIGINAL,
            Decision.CONFIRM_AND_CLEAN_ORIGINAL,
        ),
        (Phase.PUBLISHING, File.VERIFIED_COPY, File.ABSENT, File.ABSENT, Decision.STOP),
        (
            Phase.PUBLISHING,
            File.VERIFIED_COPY,
            File.VERIFIED_COPY,
            File.ORIGINAL,
            Decision.STOP,
        ),
        (
            Phase.CONFIRMED,
            File.VERIFIED_COPY,
            File.ABSENT,
            File.ORIGINAL,
            Decision.CONFIRM_AND_CLEAN_ORIGINAL,
        ),
        (
            Phase.CONFIRMED,
            File.VERIFIED_COPY,
            File.ABSENT,
            File.ABSENT,
            Decision.COMPLETE,
        ),
        (Phase.CONFIRMED, File.UNKNOWN, File.ABSENT, File.ORIGINAL, Decision.STOP),
        (Phase.CANCELLED, File.ORIGINAL, File.ABSENT, File.ABSENT, Decision.COMPLETE),
    ],
)
def test_publication_decision(
    phase: Phase, source: File, work: File, saved: File, expected: Decision
) -> None:
    assert reconcile_publication(phase, source, work, saved) is expected
