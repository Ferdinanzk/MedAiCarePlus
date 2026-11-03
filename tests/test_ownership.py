"""The hand-to-body rule (3 Nov 2026): select_owned_observations ties the patient's face to one body and the body's
wrists to hands, without refusing a crowded frame, with a pose seen twice merged, with an unclear hand skipped on
its own, and with a memory (OwnershipMemory) that carries a confirmed hand, and the patient's body, across the
frames where this frame's own vote fails. Pure function tests, packet geometry only."""

import sys
import types

import pytest

_asyncpg_stub = types.ModuleType("asyncpg")
_asyncpg_stub.Pool = object
sys.modules.setdefault("asyncpg", _asyncpg_stub)

from app.services.monitor_service import OwnershipMemory, select_owned_observations  # noqa: E402

BOX = [0.4, 0.3, 0.2, 0.3]                      # the patient's face
CENTRE = (0.5, 0.45)


def face(box=BOX):
    return {"box": list(box), "points": [[box[0] + box[2] / 2, box[1] + box[3] / 2]] * 9}


def pose(nose, shoulders_mid, wrists, width=0.25, visibility=0.9):
    return {"nose": list(nose),
            "shoulders": [[shoulders_mid[0] - width / 2, shoulders_mid[1]], [shoulders_mid[0] + width / 2, shoulders_mid[1]]],
            "wrists": [[w[0], w[1], visibility if len(w) < 3 else w[2]] for w in wrists]}


def hand(root):
    return [list(root)] * 13


PATIENT = pose(CENTRE, (0.5, 0.75), [(0.62, 0.7)])          # her wrist at (0.62, 0.70)
NEIGHBOUR = pose((0.9, 0.3), (0.9, 0.6), [(0.8, 0.5)])      # someone to the right, wrist at (0.80, 0.50)
HAND = hand((0.63, 0.71))                                   # on the patient's wrist


def select(faces, poses, hands, memory=None, timestamp=1000.0, explain=False):
    return select_owned_observations(faces, poses, hands, BOX, memory=memory, timestamp=timestamp, explain=explain)


def test_the_plain_case_ties_the_hand_to_the_patient():
    selected = select([face()], [PATIENT, NEIGHBOUR], [HAND, hand((0.81, 0.51))])
    assert selected["hands"] == [HAND] and selected["hand_indices"] == [0]
    assert selected["carried"] is False and selected["carried_hands"] == [False]
    assert selected["pose_index"] == 0 and selected["shoulders"] == pytest.approx((0.5, 0.75))


def test_a_crowd_no_longer_refuses_the_frame():
    others = [pose((0.1, 0.3), (0.1, 0.6), [(0.15, 0.5)]), pose((0.9, 0.9), (0.9, 0.95), [(0.95, 0.9)])]
    many_hands = [hand((0.05 * k, 0.9)) for k in range(7)]
    selected = select([face()] + [face([0.0, 0.0, 0.05, 0.05])] * 3, [PATIENT, NEIGHBOUR] + others, [HAND] + many_hands)
    assert selected is not None and selected["hands"] == [HAND]


def test_a_pose_seen_twice_is_one_person():
    """A second pose with its nose a hair from the patient's used to be 'two bodies for the face': now it is the
    same person, and the wider shoulders are kept."""
    ghost = pose((0.52, 0.46), (0.5, 0.74), [(0.3, 0.9)], width=0.1)
    selected, reason = select([face()], [ghost, PATIENT], [HAND], explain=True)
    assert reason == "" and selected["pose_index"] == 1 and selected["hands"] == [HAND]


def test_an_unclear_hand_is_skipped_on_its_own():
    """A hand as close to a neighbour's wrist as to the patient's is undecided; the patient's other hand is still
    owned. Before, the whole frame was refused."""
    neighbour = pose((0.9, 0.3), (0.9, 0.6), [(0.33, 0.69)])
    patient = pose(CENTRE, (0.5, 0.75), [(0.62, 0.7), (0.31, 0.7)])
    unclear = hand((0.32, 0.7))
    selected = select([face()], [patient, neighbour], [unclear, HAND])
    assert selected["hands"] == [HAND] and selected["hand_indices"] == [1]


def test_no_body_gives_the_face_with_no_hands_not_a_refusal():
    selected, reason = select([face()], [NEIGHBOUR], [HAND], explain=True)
    assert selected is not None and selected["hands"] == [] and reason == "no_body"
    assert select([face()], [], [HAND]) is not None


def test_the_face_itself_still_fails_closed():
    assert select([], [PATIENT], [HAND], explain=True) == (None, "face_ambiguous")
    assert select([face(), face()], [PATIENT], [HAND], explain=True) == (None, "face_ambiguous")
    assert select_owned_observations([face()], [PATIENT], [HAND], None, explain=True) == (None, "identity")


def _confirm(memory, at=1000.0):
    selected = select([face()], [PATIENT, NEIGHBOUR], [HAND], memory=memory, timestamp=at)
    memory.update(selected, at)
    assert memory.hands and memory.hands[0][1] == at
    return selected


def test_a_confirmed_hand_is_carried_while_the_body_is_lost():
    memory = OwnershipMemory()
    _confirm(memory)
    moved = hand((0.65, 0.69))                                   # 0.03 on, a frame later
    selected = select([face()], [], [moved], memory=memory, timestamp=1000.07)
    assert selected["hands"] == [moved] and selected["carried"] is True and selected["carried_hands"] == [True]
    memory.update(selected, 1000.07)
    assert memory.hands[0][0] == pytest.approx((0.65, 0.69)) and memory.hands[0][1] == 1000.0   # confirmation kept
    later = hand((0.66, 0.68))
    assert select([face()], [], [later], memory=memory, timestamp=1002.9)["carried"] is True
    assert select([face()], [], [later], memory=memory, timestamp=1003.2)["hands"] == []     # past the carry


def test_a_carried_hand_must_move_on_smoothly():
    memory = OwnershipMemory()
    _confirm(memory)
    jumped = hand((0.3, 0.3))
    assert select([face()], [], [jumped], memory=memory, timestamp=1000.07)["hands"] == []
    far_but_later = hand((0.75, 0.6))                            # 0.17 away after 0.4 s: within 0.06 + 0.4 x 0.4
    assert select([face()], [], [far_but_later], memory=memory, timestamp=1000.4)["carried"] is True


def test_only_the_nearest_of_two_hands_continues_a_remembered_one():
    memory = OwnershipMemory()
    _confirm(memory)
    near, nearer = hand((0.66, 0.72)), hand((0.64, 0.71))
    selected = select([face()], [], [near, nearer], memory=memory, timestamp=1000.07)
    assert selected["hands"] == [nearer]


def test_a_carried_hand_survives_hidden_wrists_and_a_weak_rival_but_not_a_strong_one():
    memory = OwnershipMemory()
    _confirm(memory)
    hidden = pose(CENTRE, (0.5, 0.75), [(0.62, 0.7, 0.3)])      # the patient's wrist below the visibility floor
    assert select([face()], [hidden, NEIGHBOUR], [HAND], memory=memory, timestamp=1000.07)["carried"] is True
    weak_rival = pose((0.9, 0.3), (0.9, 0.6), [(0.63, 0.59)])    # 0.12 from the root, the patient's wrist at 0.17
    patient_far = pose(CENTRE, (0.5, 0.75), [(0.62, 0.88)])
    selected = select([face()], [patient_far, weak_rival], [HAND], memory=memory, timestamp=1000.07)
    assert selected["hands"] == [HAND] and selected["carried"] is True
    strong_rival = pose((0.9, 0.3), (0.9, 0.6), [(0.64, 0.72)])  # 0.014 from the root: that neighbour's own hand
    assert select([face()], [patient_far, strong_rival], [HAND], memory=memory, timestamp=1000.07)["hands"] == []


def test_the_body_is_carried_by_its_shoulders_when_the_nose_leaves_the_box():
    memory = OwnershipMemory()
    _confirm(memory)
    nose_off = pose((0.5, 0.75), (0.51, 0.76), [(0.62, 0.7)])    # the nose on the chin, the shoulders in place
    selected = select([face()], [nose_off, NEIGHBOUR], [HAND], memory=memory, timestamp=1000.5)
    assert selected["pose_index"] == 0 and selected["pose_carried"] is True
    assert selected["hands"] == [HAND] and selected["carried"] is True
    memory.update(selected, 1000.5)
    assert memory.shoulders_at == 1000.0                          # a carried pose doesn't refresh the shoulders
    assert select([face()], [nose_off, NEIGHBOUR], [HAND], memory=memory, timestamp=1002.5)["pose_index"] is None


def test_memory_forgets_old_hands_and_clears_on_demand():
    memory = OwnershipMemory()
    _confirm(memory)
    memory.update(None, 1004.0)
    assert memory.hands == []
    _confirm(memory, at=1005.0)
    memory.clear()
    assert memory.hands == [] and memory.shoulders is None
