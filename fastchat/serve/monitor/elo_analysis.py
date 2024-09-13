import os
import argparse
import ast
from collections import defaultdict
import datetime
import json
import math
import pickle
from pytz import timezone
from functools import partial
import multiprocessing as mp

import numpy as np
import pandas as pd
import plotly.express as px
from tqdm import tqdm
from transformers import AutoTokenizer

from fastchat.model.model_registry import get_model_info
from fastchat.serve.monitor.basic_stats import get_log_files
from fastchat.serve.monitor.clean_battle_data import clean_battle_data

pd.options.display.float_format = "{:.2f}".format


STYLE_CONTROL_ELEMENTS_V1 = [
    "sum_assistant_a_tokens",
    "header_count_a",
    "list_count_a",
    "bold_count_a",
    "sum_assistant_b_tokens",
    "header_count_b",
    "list_count_b",
    "bold_count_b",
]


def compute_elo(battles, K=4, SCALE=400, BASE=10, INIT_RATING=1000):
    rating = defaultdict(lambda: INIT_RATING)

    for rd, model_a, model_b, winner in battles[
        ["model_a", "model_b", "winner"]
    ].itertuples():
        ra = rating[model_a]
        rb = rating[model_b]
        ea = 1 / (1 + BASE ** ((rb - ra) / SCALE))
        eb = 1 / (1 + BASE ** ((ra - rb) / SCALE))
        if winner == "model_a":
            sa = 1
        elif winner == "model_b":
            sa = 0
        elif winner == "tie" or winner == "tie (bothbad)":
            sa = 0.5
        else:
            raise Exception(f"unexpected vote {winner}")
        rating[model_a] += K * (sa - ea)
        rating[model_b] += K * (1 - sa - eb)

    return dict(rating)


def get_bootstrap_result(battles, func_compute_elo, num_round=1000):
    rows = []
    for i in tqdm(range(num_round), desc="bootstrap"):
        tmp_battles = battles.sample(frac=1.0, replace=True)
        rows.append(func_compute_elo(tmp_battles))
    df = pd.DataFrame(rows)
    return df[df.median().sort_values(ascending=False).index]


def preprocess_battles_to_arrays(df):
    """convert the battles df into numpy arrays optimized for BT likelihood calculation"""

    models = pd.unique(df[["model_a", "model_b"]].values.ravel()).tolist()
    model_to_idx = {model: idx for idx, model in enumerate(models)}
    # the 3 columns of schedule represent: model_a id, model_b id, outcome_id
    schedule = np.empty((len(df), 3), dtype=np.int32)
    # set the two model cols by mapping the model names to their int ids
    schedule[:, [0, 1]] = (
        df[["model_a", "model_b"]].map(lambda x: model_to_idx[x]).values
    )
    # map outcomes to integers (must be same dtype as model ids so it can be in the same array)
    # model_a win -> 2, tie -> 1, model_b win -> 0
    schedule[:, 2] = np.select(
        condlist=[df["winner"] == "model_a", df["winner"] == "model_b"],
        choicelist=[2, 0],
        default=1,
    )
    # count the number of occurances of each observed result
    matchups_outcomes, counts = np.unique(schedule, return_counts=True, axis=0)
    matchups = matchups_outcomes[:, [0, 1]]
    # map 2 -> 1.0, 1 -> 0.5, 0 -> 0.0 which will be used as labels during optimization
    outcomes = matchups_outcomes[:, 2].astype(np.float64) / 2.0
    # each possible result is weighted according to number of times it occured in the dataset
    weights = counts.astype(np.float64)
    return matchups, outcomes, weights, models


def bt_loss_and_grad(ratings, matchups, outcomes, weights, alpha=1.0):
    """negative log likelihood and gradient for BT model with numpy array inputs"""
    from scipy.special import expit as sigmoid

    matchup_ratings = ratings[matchups]
    logits = alpha * (matchup_ratings[:, 0] - matchup_ratings[:, 1])
    probs = sigmoid(logits)
    # this form naturally counts a draw as half a win and half a loss
    loss = -(
        (np.log(probs) * outcomes + np.log(1.0 - probs) * (1.0 - outcomes)) * weights
    ).sum()
    matchups_grads = -alpha * (outcomes - probs) * weights
    model_grad = np.zeros_like(ratings)
    # aggregate gradients at the model level using the indices in matchups
    np.add.at(
        model_grad,
        matchups[:, [0, 1]],
        matchups_grads[:, None] * np.array([1.0, -1.0], dtype=np.float64),
    )
    return loss, model_grad


def fit_bt(matchups, outcomes, weights, n_models, alpha, tol=1e-6):
    """perform the BT likelihood optimization"""
    from scipy.optimize import minimize

    initial_ratings = np.zeros(n_models, dtype=np.float64)
    result = minimize(
        fun=bt_loss_and_grad,
        x0=initial_ratings,
        args=(matchups, outcomes, weights, alpha),
        jac=True,
        method="L-BFGS-B",
        options={"disp": False, "maxiter": 100, "gtol": tol},
    )
    return result["x"]


def scale_and_offset(
    ratings,
    models,
    scale=400,
    init_rating=1000,
    baseline_model="mixtral-8x7b-instruct-v0.1",
    baseline_rating=1114,
):
    """convert ratings from the natural scale to the Elo rating scale with an anchored baseline"""
    scaled_ratings = (ratings * scale) + init_rating
    if baseline_model in models:
        baseline_idx = models.index(baseline_model)
        scaled_ratings += baseline_rating - scaled_ratings[..., [baseline_idx]]
    return scaled_ratings


def compute_elo_mle_with_tie(
    df,
    SCALE=400,
    BASE=10,
    INIT_RATING=1000,
    baseline_model="mixtral-8x7b-instruct-v0.1",
    baseline_rating=1114.0,
):
    matchups, outcomes, weights, models = preprocess_battles_to_arrays(df)
    ratings = fit_bt(matchups, outcomes, weights, len(models), np.log(BASE))
    scaled_ratings = scale_and_offset(
        ratings, models, SCALE, INIT_RATING, baseline_model, baseline_rating
    )
    return pd.Series(scaled_ratings, index=models).sort_values(ascending=False)


def get_bootstrap_result_elo_mle_with_tie(
    df, num_round, BASE=10.0, SCALE=400.0, INIT_RATING=1000.0
):
    matchups, outcomes, weights, models = preprocess_battles_to_arrays(battles)
    # bootstrap sample the unique outcomes and their counts directly using the multinomial distribution
    idxs = np.random.multinomial(
        n=len(battles), pvals=weights / weights.sum(), size=(num_round)
    )
    # only the distribution over their occurance counts changes between samples (and it can be 0)
    boot_weights = idxs.astype(np.float64) / len(battles)

    # the only thing different across samples is the distribution of weights
    bt_fn = partial(
        fit_bt, matchups, outcomes, n_models=len(models), alpha=np.log(BASE)
    )

    with mp.Pool(os.cpu_count()) as pool:
        results = pool.map(bt_fn, boot_weights)

    ratings = np.array(results)
    scaled_ratings = scale_and_offset(ratings, models, SCALE, INIT_RATING)
    df = pd.DataFrame(scaled_ratings, columns=models)
    return df[df.median().sort_values(ascending=False).index]


def get_median_elo_from_bootstrap(bootstrap_df):
    median = dict(bootstrap_df.quantile(0.5))
    median = {k: int(v + 0.5) for k, v in median.items()}
    return median


def compute_pairwise_win_fraction(battles, model_order, limit_show_number=None):
    # Times each model wins as Model A
    a_win_ptbl = pd.pivot_table(
        battles[battles["winner"] == "model_a"],
        index="model_a",
        columns="model_b",
        aggfunc="size",
        fill_value=0,
    )

    # Table counting times each model wins as Model B
    b_win_ptbl = pd.pivot_table(
        battles[battles["winner"] == "model_b"],
        index="model_a",
        columns="model_b",
        aggfunc="size",
        fill_value=0,
    )

    # Table counting number of A-B pairs
    num_battles_ptbl = pd.pivot_table(
        battles, index="model_a", columns="model_b", aggfunc="size", fill_value=0
    )

    # Computing the proportion of wins for each model as A and as B
    # against all other models
    row_beats_col_freq = (a_win_ptbl + b_win_ptbl.T) / (
        num_battles_ptbl + num_battles_ptbl.T
    )

    if model_order is None:
        prop_wins = row_beats_col_freq.mean(axis=1).sort_values(ascending=False)
        model_order = list(prop_wins.keys())

    if limit_show_number is not None:
        model_order = model_order[:limit_show_number]

    # Arrange ordering according to proprition of wins
    row_beats_col = row_beats_col_freq.loc[model_order, model_order]
    return row_beats_col


def visualize_leaderboard_table(rating):
    models = list(rating.keys())
    models.sort(key=lambda k: -rating[k])

    emoji_dict = {
        1: "🥇",
        2: "🥈",
        3: "🥉",
    }

    md = ""
    md += "| Rank | Model | Elo Rating | Description |\n"
    md += "| --- | --- | --- | --- |\n"
    for i, model in enumerate(models):
        rank = i + 1
        minfo = get_model_info(model)
        emoji = emoji_dict.get(rank, "")
        md += f"| {rank} | {emoji} [{model}]({minfo.link}) | {rating[model]:.0f} | {minfo.description} |\n"

    return md


def visualize_pairwise_win_fraction(battles, model_order, scale=1):
    row_beats_col = compute_pairwise_win_fraction(battles, model_order)
    fig = px.imshow(
        row_beats_col,
        color_continuous_scale="RdBu",
        text_auto=".2f",
        height=700 * scale,
        width=700 * scale,
    )
    fig.update_layout(
        xaxis_title="Model B",
        yaxis_title="Model A",
        xaxis_side="top",
        title_y=0.07,
        title_x=0.5,
    )
    fig.update_traces(
        hovertemplate="Model A: %{y}<br>Model B: %{x}<br>Fraction of A Wins: %{z}<extra></extra>"
    )

    return fig


def visualize_battle_count(battles, model_order, scale=1):
    ptbl = pd.pivot_table(
        battles, index="model_a", columns="model_b", aggfunc="size", fill_value=0
    )
    battle_counts = ptbl + ptbl.T
    fig = px.imshow(
        battle_counts.loc[model_order, model_order],
        text_auto=True,
        height=700 * scale,
        width=700 * scale,
    )
    fig.update_layout(
        xaxis_title="Model B",
        yaxis_title="Model A",
        xaxis_side="top",
        title_y=0.07,
        title_x=0.5,
    )
    fig.update_traces(
        hovertemplate="Model A: %{y}<br>Model B: %{x}<br>Count: %{z}<extra></extra>"
    )
    return fig


def visualize_average_win_rate(battles, limit_show_number, scale=1):
    row_beats_col_freq = compute_pairwise_win_fraction(
        battles, None, limit_show_number=limit_show_number
    )
    fig = px.bar(
        row_beats_col_freq.mean(axis=1).sort_values(ascending=False),
        text_auto=".2f",
        height=500 * scale,
        width=700 * scale,
    )
    fig.update_layout(
        yaxis_title="Average Win Rate", xaxis_title="Model", showlegend=False
    )
    return fig


def visualize_bootstrap_elo_rating(df, df_final, limit_show_number, scale=1):
    bars = (
        pd.DataFrame(
            dict(
                lower=df.quantile(0.025),
                rating=df_final,
                upper=df.quantile(0.975),
            )
        )
        .reset_index(names="model")
        .sort_values("rating", ascending=False)
    )
    bars = bars[:limit_show_number]
    bars["error_y"] = bars["upper"] - bars["rating"]
    bars["error_y_minus"] = bars["rating"] - bars["lower"]
    bars["rating_rounded"] = np.round(bars["rating"])
    fig = px.scatter(
        bars,
        x="model",
        y="rating",
        error_y="error_y",
        error_y_minus="error_y_minus",
        text="rating_rounded",
        height=700,
        width=700 * scale,
    )
    fig.update_layout(xaxis_title="Model", yaxis_title="Rating")
    return fig


def limit_user_votes(battles, daily_vote_per_user):
    from datetime import datetime

    print("Before limiting user votes: ", len(battles))
    # add date
    battles["date"] = battles["tstamp"].apply(
        lambda x: datetime.fromtimestamp(x).strftime("%Y-%m-%d")
    )

    battles_new = pd.DataFrame()
    for date in battles["date"].unique():
        # only take the first daily_vote_per_user votes per judge per day
        df_today = battles[battles["date"] == date]
        df_sub = df_today.groupby("judge").head(daily_vote_per_user)

        # add df_sub to a new dataframe
        battles_new = pd.concat([battles_new, df_sub])
    print("After limiting user votes: ", len(battles_new))
    return battles_new


def get_model_pair_stats(battles):
    battles["ordered_pair"] = battles.apply(
        lambda x: tuple(sorted([x["model_a"], x["model_b"]])), axis=1
    )

    model_pair_stats = {}

    for index, row in battles.iterrows():
        pair = row["ordered_pair"]
        if pair not in model_pair_stats:
            model_pair_stats[pair] = {"win": 0, "loss": 0, "tie": 0}

        if row["winner"] in ["tie", "tie (bothbad)"]:
            model_pair_stats[pair]["tie"] += 1
        elif row["winner"] == "model_a" and row["model_a"] == min(pair):
            model_pair_stats[pair]["win"] += 1
        elif row["winner"] == "model_b" and row["model_b"] == min(pair):
            model_pair_stats[pair]["win"] += 1
        else:
            model_pair_stats[pair]["loss"] += 1

    return model_pair_stats


def outlier_detect(
    model_pair_stats,
    battles,
    max_vote=100,
    randomized=False,
    alpha=0.05,
    c_param=0.5,
    user_list=None,
):
    if user_list is None:
        # only check user who has >= 5 votes to save compute
        user_vote_cnt = battles["judge"].value_counts()
        user_list = user_vote_cnt[user_vote_cnt >= 5].index.tolist()
    print("#User to be checked: ", len(user_list))

    bad_user_list = []
    for user in user_list:
        flag = False
        p_upper = []
        p_lower = []
        df_2 = battles[battles["judge"] == user]
        for row in df_2.iterrows():
            if len(p_upper) >= max_vote:
                break

            model_pair = tuple(sorted([row[1]["model_a"], row[1]["model_b"]]))

            if row[1]["winner"] in ["tie", "tie (bothbad)"]:
                vote = 0.5
            elif row[1]["winner"] == "model_a" and row[1]["model_a"] == model_pair[0]:
                vote = 1
            elif row[1]["winner"] == "model_b" and row[1]["model_b"] == model_pair[0]:
                vote = 1
            else:
                vote = 0

            stats = model_pair_stats[model_pair]
            # count all votes
            # ratings = np.array(
            #     [1] * stats["win"] + [0.5] * stats["tie"] + [0] * stats["loss"]
            # )

            # only count win and loss
            ratings = np.array([1] * stats["win"] + [0] * stats["loss"])
            if randomized:
                noise = np.random.uniform(-1e-5, 1e-5, len(ratings))
                ratings += noise
                vote += np.random.uniform(-1e-5, 1e-5)

            p_upper += [(ratings <= vote).mean()]
            p_lower += [(ratings >= vote).mean()]

            M_upper = np.prod(1 / (2 * np.array(p_upper)))
            M_lower = np.prod(1 / (2 * np.array(p_lower)))

            # M_upper = np.prod((1 - c_param) / (c_param * np.array(p_upper) ** c_param))
            # M_lower = np.prod((1 - c_param) / (c_param * np.array(p_lower) ** c_param))
            if (M_upper > 1 / alpha) or (M_lower > 1 / alpha):
                print(f"Identify bad user with {len(p_upper)} votes")
                flag = True
                break
        if flag:
            bad_user_list.append({"user_id": user, "votes": len(p_upper)})
    print("Bad user length: ", len(bad_user_list))
    print(bad_user_list)

    bad_user_id_list = [x["user_id"] for x in bad_user_list]
    # remove bad users
    battles = battles[~battles["judge"].isin(bad_user_id_list)]
    return battles


def fit_mle_elo(X, Y, models, indices=None, SCALE=400, INIT_RATING=1000):
    from sklearn.linear_model import LogisticRegression

    p = len(models.index)

    lr = LogisticRegression(fit_intercept=False)
    if indices:
        lr.fit(X[indices], Y[indices])
    else:
        lr.fit(X, Y)

    elo_scores = SCALE * lr.coef_[0] + INIT_RATING
    # calibrate llama-13b to 800 if applicable
    if "mixtral-8x7b-instruct-v0.1" in models.index:
        elo_scores += 1114 - elo_scores[models["mixtral-8x7b-instruct-v0.1"]]
    return (
        pd.Series(elo_scores[:p], index=models.index).sort_values(ascending=False),
        lr.coef_[0][p:],
    )


def construct_style_matrices(
    df,
    BASE=10,
    apply_ratio=[1, 1, 1, 1],
    style_elements=STYLE_CONTROL_ELEMENTS_V1,
    add_one=True,
):
    models = pd.concat([df["model_a"], df["model_b"]]).unique()
    models = pd.Series(np.arange(len(models)), index=models)

    # duplicate battles
    df = pd.concat([df, df], ignore_index=True)
    p = len(models.index)
    n = df.shape[0]
    assert len(style_elements) % 2 == 0
    k = int(len(style_elements) / 2)

    X = np.zeros([n, p + k])
    X[np.arange(n), models[df["model_a"]]] = +math.log(BASE)
    X[np.arange(n), models[df["model_b"]]] = -math.log(BASE)

    # creates turn each of the specified column in "conv_metadata" into a vector
    style_vector = np.array(
        [
            df.conv_metadata.map(
                lambda x: x[element]
                if type(x[element]) is int
                else sum(x[element].values())
            ).tolist()
            for element in style_elements
        ]
    )

    style_diff = (style_vector[:k] - style_vector[k:]).astype(float)
    style_sum = (style_vector[:k] + style_vector[k:]).astype(float)

    if add_one:
        style_sum = style_sum + np.ones(style_diff.shape)

    apply_ratio = np.flatnonzero(apply_ratio)

    style_diff[apply_ratio] /= style_sum[
        apply_ratio
    ]  # Apply ratio where necessary (length, etc)

    style_mean = np.mean(style_diff, axis=1)
    style_std = np.std(style_diff, axis=1)

    X[:, -k:] = ((style_diff - style_mean[:, np.newaxis]) / style_std[:, np.newaxis]).T

    # one A win => two A win
    Y = np.zeros(n)
    Y[df["winner"] == "model_a"] = 1.0

    # one tie => one A win + one B win
    # find tie + tie (both bad) index
    tie_idx = (df["winner"] == "tie") | (df["winner"] == "tie (bothbad)")
    tie_idx[len(tie_idx) // 2 :] = False
    Y[tie_idx] = 1.0

    return X, Y, models


def get_bootstrap_result_style_control(
    X, Y, battles, models, func_compute_elo, num_round=1000
):
    elos = []
    coefs = []
    assert X.shape[0] % 2 == 0 and X.shape[0] == Y.shape[0]
    k = int(
        X.shape[0] / 2
    )  # Since we duplicate the battles when constructing X and Y, we don't want to sample the duplicates

    battles_tie_idx = (battles["winner"] == "tie") | (
        battles["winner"] == "tie (bothbad)"
    )
    for _ in tqdm(range(num_round), desc="bootstrap"):
        indices = np.random.choice(list(range(k)), size=(k), replace=True)

        index2tie = np.zeros(k, dtype=bool)
        index2tie[battles_tie_idx] = True

        nontie_indices = indices[~index2tie[indices]]
        tie_indices = np.concatenate(
            [indices[index2tie[indices]], indices[index2tie[indices]] + k]
        )

        _X = np.concatenate([X[nontie_indices], X[nontie_indices], X[tie_indices]])
        _Y = np.concatenate([Y[nontie_indices], Y[nontie_indices], Y[tie_indices]])

        assert _X.shape == X.shape and _Y.shape == Y.shape

        states = ~_X[:, : len(models)].any(axis=0)

        elo, coef = func_compute_elo(_X, _Y, models=models[~states])
        elos.append(elo)
        coefs.append(coef)

    df = pd.DataFrame(elos)
    return df[df.median().sort_values(ascending=False).index], coefs


def filter_long_conv(row):
    threshold = 768
    for conversation_type in ["conversation_a", "conversation_b"]:
        cur_conv = row[conversation_type]
        num_tokens_all = sum([turn["num_tokens"] for turn in cur_conv])
        if num_tokens_all >= threshold:
            return True
    return False


def report_elo_analysis_results(
    battles_json,
    rating_system="bt",
    num_bootstrap=100,
    exclude_models=[],
    langs=[],
    exclude_tie=False,
    exclude_unknown_lang=False,
    daily_vote_per_user=None,
    run_outlier_detect=False,
    scale=1,
    filter_func=lambda x: True,
    style_control=False,
):
    battles = pd.DataFrame(battles_json)

    tqdm.pandas(desc=f"Processing using {filter_func.__name__}")
    filtered_indices = battles.progress_apply(filter_func, axis=1)
    battles = battles[filtered_indices]

    battles = battles.sort_values(ascending=True, by=["tstamp"])

    if len(langs) > 0:
        battles = battles[battles["language"].isin(langs)]
    if exclude_unknown_lang:
        battles = battles[~battles["language"].str.contains("unknown")]

    # remove excluded models
    battles = battles[
        ~(
            battles["model_a"].isin(exclude_models)
            | battles["model_b"].isin(exclude_models)
        )
    ]

    # Only use anonymous votes
    battles = battles[battles["anony"]].reset_index(drop=True)
    battles_no_ties = battles[~battles["winner"].str.contains("tie")]
    if exclude_tie:
        battles = battles_no_ties

    if daily_vote_per_user is not None:
        battles = limit_user_votes(battles, daily_vote_per_user)

    if run_outlier_detect:
        model_pair_stats = get_model_pair_stats(battles)
        battles = outlier_detect(model_pair_stats, battles)

    print(f"Number of battles: {len(battles)}")
    # Online update
    elo_rating_online = compute_elo(battles)

    if rating_system == "bt":
        if style_control:
            X, Y, models = construct_style_matrices(battles)
            bootstrap_df, boostrap_coef = get_bootstrap_result_style_control(
                X, Y, battles, models, fit_mle_elo, num_round=num_bootstrap
            )
            elo_rating_final, coef_final = fit_mle_elo(X, Y, models)
        else:
            bootstrap_df = get_bootstrap_result_elo_mle_with_tie(
                battles, num_round=num_bootstrap
            )
            elo_rating_final = compute_elo_mle_with_tie(battles)
    elif rating_system == "elo":
        bootstrap_df = get_bootstrap_result(
            battles, compute_elo, num_round=num_bootstrap
        )
        elo_rating_median = get_median_elo_from_bootstrap(bootstrap_df)
        elo_rating_final = elo_rating_median

    model_order = list(elo_rating_final.keys())

    model_rating_q025 = bootstrap_df.quantile(0.025)
    model_rating_q975 = bootstrap_df.quantile(0.975)

    # compute ranking based on CI
    ranking = {}
    for i, model_a in enumerate(model_order):
        ranking[model_a] = 1
        for j, model_b in enumerate(model_order):
            if i == j:
                continue
            if model_rating_q025[model_b] > model_rating_q975[model_a]:
                ranking[model_a] += 1

    # leaderboard_table_df: elo rating, variance, 95% interval, number of battles
    leaderboard_table_df = pd.DataFrame(
        {
            "rating": elo_rating_final,
            "variance": bootstrap_df.var(),
            "rating_q975": bootstrap_df.quantile(0.975),
            "rating_q025": bootstrap_df.quantile(0.025),
            "num_battles": battles["model_a"]
            .value_counts()
            .add(battles["model_b"].value_counts(), fill_value=0),
            "final_ranking": pd.Series(ranking),
        }
    )

    model_order.sort(key=lambda k: -elo_rating_final[k])
    limit_show_number = int(25 * scale)
    model_order = model_order[:limit_show_number]

    # Plots
    leaderboard_table = visualize_leaderboard_table(elo_rating_final)
    win_fraction_heatmap = visualize_pairwise_win_fraction(
        battles_no_ties, model_order, scale=scale
    )
    battle_count_heatmap = visualize_battle_count(
        battles_no_ties, model_order, scale=scale
    )
    average_win_rate_bar = visualize_average_win_rate(
        battles_no_ties, limit_show_number, scale=scale
    )
    bootstrap_elo_rating = visualize_bootstrap_elo_rating(
        bootstrap_df, elo_rating_final, limit_show_number, scale=scale
    )

    last_updated_tstamp = battles["tstamp"].max()
    last_updated_datetime = datetime.datetime.fromtimestamp(
        last_updated_tstamp, tz=timezone("US/Pacific")
    ).strftime("%Y-%m-%d %H:%M:%S %Z")

    return {
        "rating_system": rating_system,
        "elo_rating_online": elo_rating_online,
        "elo_rating_final": elo_rating_final,
        "leaderboard_table": leaderboard_table,
        "win_fraction_heatmap": win_fraction_heatmap,
        "battle_count_heatmap": battle_count_heatmap,
        "average_win_rate_bar": average_win_rate_bar,
        "bootstrap_elo_rating": bootstrap_elo_rating,
        "last_updated_datetime": last_updated_datetime,
        "last_updated_tstamp": last_updated_tstamp,
        "bootstrap_df": bootstrap_df,
        "leaderboard_table_df": leaderboard_table_df,
        "style_coefficients": {
            "bootstrap": np.vstack(boostrap_coef),
            "final": coef_final,
        }
        if rating_system == "bt" and style_control
        else {},
    }


def pretty_print_elo_rating(rating):
    model_order = list(rating.keys())
    model_order.sort(key=lambda k: -rating[k])
    for i, model in enumerate(model_order):
        print(f"{i+1:2d}, {model:25s}, {rating[model]:.0f}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--clean-battle-file", type=str)
    parser.add_argument("--max-num-files", type=int)
    parser.add_argument("--num-bootstrap", type=int, default=100)
    parser.add_argument(
        "--rating-system", type=str, choices=["bt", "elo"], default="bt"
    )
    parser.add_argument("--exclude-models", type=str, nargs="+", default=[])
    parser.add_argument("--exclude-tie", action="store_true", default=False)
    parser.add_argument("--exclude-unknown-lang", action="store_true", default=False)
    parser.add_argument("--exclude-url", action="store_true", default=False)
    parser.add_argument("--langs", type=str, nargs="+", default=[])
    parser.add_argument("--daily-vote-per-user", type=int, default=None)
    parser.add_argument("--run-outlier-detect", action="store_true", default=False)
    parser.add_argument("--category", nargs="+", default=["full"])
    parser.add_argument("--scale", type=float, default=1)
    parser.add_argument("--style-control", action="store_true")
    args = parser.parse_args()

    np.random.seed(42)

    if args.clean_battle_file:
        # Read data from a cleaned battle files
        battles = pd.read_json(args.clean_battle_file)
    else:
        # Read data from all log files
        log_files = get_log_files(args.max_num_files)
        battles = clean_battle_data(log_files)

    filter_func_map = {
        "full": lambda x: True,
        "long": filter_long_conv,
        "chinese": lambda x: x["language"] == "Chinese",
        "english": lambda x: x["language"] == "English",
    }
    assert all(
        [cat in filter_func_map for cat in args.category]
    ), f"Invalid category: {args.category}"

    results = {}
    for cat in args.category:
        filter_func = filter_func_map[cat]
        results[cat] = report_elo_analysis_results(
            battles,
            rating_system=args.rating_system,
            num_bootstrap=args.num_bootstrap,
            exclude_models=args.exclude_models,
            langs=args.langs,
            exclude_tie=args.exclude_tie,
            exclude_unknown_lang=args.exclude_unknown_lang,
            daily_vote_per_user=args.daily_vote_per_user,
            run_outlier_detect=args.run_outlier_detect,
            scale=args.scale,
            filter_func=filter_func,
            style_control=args.style_control,
        )

    for cat in args.category:
        print(f"# Results for {cat} conversations")
        print("# Online Elo")
        pretty_print_elo_rating(results[cat]["elo_rating_online"])
        print("# Median")
        pretty_print_elo_rating(results[cat]["elo_rating_final"])
        print(f"last update : {results[cat]['last_updated_datetime']}")

        last_updated_tstamp = results[cat]["last_updated_tstamp"]
        cutoff_date = datetime.datetime.fromtimestamp(
            last_updated_tstamp, tz=timezone("US/Pacific")
        ).strftime("%Y%m%d")
        print(f"last update : {cutoff_date}")

    with open(f"elo_results_{cutoff_date}.pkl", "wb") as fout:
        pickle.dump(results, fout)
