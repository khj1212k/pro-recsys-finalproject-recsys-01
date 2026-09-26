from evaluation.recsys.ebnerd.make_report import pareto_front


def test_pareto_front_keeps_only_non_dominated_points():
    pts = [(0.30, 0.50, 0.2), (0.29, 0.60, 0.2), (0.28, 0.55, 0.1), (0.30, 0.50, 0.2)]
    # 3번째는 2번째에 모든 축에서 지배된다. 1·4번째는 같은 점이라 서로 지배하지 않는다.
    assert pareto_front(pts) == [True, True, False, True]


def test_pareto_front_single_objective_tie_is_kept():
    assert pareto_front([(1.0,), (1.0,), (0.5,)]) == [True, True, False]
