from app.services.face_login import FaceBox, FrameVerdict, decide, main_face

KW = dict(min_area_ratio=0.05, second_ratio=0.70)


def box(w, h):
    return FaceBox(0, 0, w, h, 0.9)


def v(code, label=None, distance=None, liveness=None):
    return FrameVerdict(code, label, distance, liveness, 1)


def m(distance=0.1, liveness=None, label="ferdi"):
    return v("match", label, distance, liveness)


def dec(vs, mode="log"):
    return decide(vs, min_matches=3, liveness_mode=mode, liveness_threshold=0.5)


def test_largest_face_wins():
    big = box(100, 100)
    assert main_face([box(60, 60), big], 320, 240, **KW) == ("ok", big)


def test_two_faces_at_070_not_at_069():
    main = box(100, 100)
    assert main_face([main, box(70, 100)], 320, 240, **KW)[0] == "two_faces"
    assert main_face([main, box(69, 100)], 320, 240, **KW)[0] == "ok"


def test_selfie_of_8_nov_is_two_faces():
    code, b = main_face([box(297, 376), box(331, 450)], 1280, 960, **KW)
    assert code == "two_faces" and b.w == 331


def test_small_face_is_come_closer():
    assert main_face([box(100, 100)], 1280, 960, **KW)[0] == "come_closer"
    # exactly 0.05 of a 100x100 frame
    code, _ = main_face([box(50, 10)], 100, 100, **KW)
    assert code == "ok"


def test_no_box_is_no_face():
    assert main_face([], 640, 480, **KW) == ("no_face", None)


def test_three_of_five_logs_in():
    d = dec([m(), m(), m(), v("unknown"), v("no_face")])
    assert d.identified and d.label == "ferdi" and d.reason == "match" and d.frames == 5


def test_two_of_five_does_not():
    d = dec([m(), m(), v("unknown"), v("unknown"), v("unknown")])
    assert not d.identified and d.reason == "unknown" and d.label is None


def test_one_two_faces_frame_fails():
    d = dec([m(), m(), m(), m(), v("two_faces")])
    assert not d.identified and d.reason == "two_faces"


def test_not_live_blocks_only_in_block_mode():
    vs = [m(liveness=0.9)] * 4 + [m(liveness=0.1)]
    assert dec(vs, "block").reason == "not_live"
    assert not dec(vs, "block").identified
    assert dec(vs, "log").identified


def test_block_mode_missing_liveness_is_not_live():
    assert dec([m(liveness=0.9), m(liveness=0.9), m()], "block").reason == "not_live"


def test_block_mode_ignores_frames_without_a_face():
    vs = [m(liveness=0.9)] * 3 + [v("no_face"), v("no_face")]
    assert dec(vs, "block").identified


def test_another_registered_person_fails_the_login():
    d = dec([m()] * 3 + [m(label="aisha")] * 2)
    assert not d.identified and d.reason == "unknown"


def test_reason_ties():
    d = dec([m(), v("come_closer"), v("come_closer"), v("unknown"), v("unknown")])
    assert d.reason == "come_closer"
    d = dec([v("no_face"), v("unknown")])
    assert d.reason == "no_face"


def test_distance_is_the_smallest_match():
    d = dec([m(0.2), m(0.05), m(0.15), v("unknown")])
    assert d.distance == 0.05


def test_face_count_is_the_most_faces_in_one_frame():
    two = FrameVerdict("two_faces", face_count=2)
    assert dec([m(), m(), two, v("no_face")]).face_count == 2
    assert dec([m(), m(), m()]).face_count == 1
    assert dec([]).face_count == 0
