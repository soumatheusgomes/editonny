"""Small explicit contracts for filesystem and decision-boundary tests."""
from pathlib import Path

# An absolute path on every platform: Windows needs the drive of the current directory.
APPROVED_ROOT = Path(Path.cwd().anchor, 'approved', 'workspace')


def make_link(test, link, target, directory=False):
    """Create a symlink. Windows without symlink privilege (error 1314) gets a junction for a
    directory, which redirects the path the same way, and skips the test for a file."""
    try:
        Path(link).symlink_to(target, target_is_directory=directory)
    except OSError as error:
        if getattr(error, 'winerror', None) != 1314:
            raise
        if not directory:
            test.skipTest('File symlinks need Developer Mode or administrator rights on Windows')
        import _winapi
        _winapi.CreateJunction(str(Path(target).absolute()), str(link))


def config(root=APPROVED_ROOT):
    return {'title': 'Interview', 'style': 'paper', 'format': '9:16', 'edit': '../edit',
            'captions': {'enabled': True, 'style': 'standard', 'maxWords': 3, 'inFrame': False},
            'blocks': [{'scene': 'fullscreen', 'start': 0, 'zoom': 1.0}],
            'workflow': {'schemaVersion': 1, 'sourceIds': ['source'],
                'language': {'conversation': 'pt-BR', 'transcription': 'auto', 'captions': 'source', 'translation': None},
                'editing': {'scope': 'full', 'intervals': [], 'protectedIntervals': [], 'cleanup': 'standard'},
                'video': {'width': 1080, 'height': 1920, 'frameRate': '30000/1001', 'timing': 'cfr', 'fit': 'preserve'},
                'color': {'mode': 'preserve', 'target': 'source', 'inputOverride': None, 'grade': None, 'conversion': None},
                'encoding': {'container': 'mp4', 'videoCodec': 'auto', 'bitDepth': 'source', 'chroma': 'source'},
                'audio': {'mode': 'normalize', 'targetLufs': -14.0, 'truePeakLimit': -1.0,
                          'processingPeak': -1.5, 'denoise': 'off', 'music': False, 'effects': False, 'tracks': 'source'},
                'visuals': {'scenes': 'off', 'allowedSceneIds': [], 'cameraMotion': False,
                            'transitions': 'off', 'backgroundTreatment': 'none'},
                'review': {'sample': 'agent'},
                'delivery': {'directory': '../../../exports', 'workspaceRoot': str(root), 'executionContext': 'local',
                             'basename': 'interview', 'subtitleFormats': [], 'variants': []}}}


def confirmation(contract, manifest):
    from project_contract import settings_hash, source_identity_hash
    return {'kind': 'initial', 'approved': True, 'settingsHash': settings_hash(contract),
            'sourceIdentityHash': source_identity_hash(manifest), 'summaryId': 'summary-1',
            'responseTo': 'summary-1', 'summary': 'Complete settings summary.',
            'summaryMessageId': 'assistant-1', 'responseMessageId': 'user-2', 'response': 'Approved.',
            'presentedAt': '2026-10-19T12:00:00+00:00', 'respondedAt': '2026-10-19T12:01:00+00:00'}
