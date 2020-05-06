import os
import time, tqdm
import numpy as np
import os.path as osp
from collections import deque
from baselines.common import explained_variance, set_global_seeds
from baselines.common.policies import build_policy

try:
    from mpi4py import MPI
except ImportError:
    MPI = None
from baselines.ppo2.runner import Runner
from collections import defaultdict


def constfn(val):
    def f(_):
        return val
    return f

def learn(*, network, env, total_timesteps, early_stopping = False, eval_env = None, seed=None, nsteps=2048, ent_coef=0.0, lr=3e-4,
            vf_coef=0.5,  max_grad_norm=0.5, gamma=0.99, lam=0.95,
            log_interval=10, nminibatches=4, noptepochs=4, cliprange=0.2,
            save_interval=0, load_path=None, model_fn=None, scope='', **network_kwargs):
    '''
    Learn policy using PPO algorithm (https://arxiv.org/abs/1707.06347)

    Parameters:
    ----------

    network:                          policy network architecture. Either string (mlp, lstm, lnlstm, cnn_lstm, cnn, cnn_small, conv_only - see baselines.common/models.py for full list)
                                      specifying the standard network architecture, or a function that takes tensorflow tensor as input and returns
                                      tuple (output_tensor, extra_feed) where output tensor is the last network layer output, extra_feed is None for feed-forward
                                      neural nets, and extra_feed is a dictionary describing how to feed state into the network for recurrent neural nets.
                                      See common/models.py/lstm for more details on using recurrent nets in policies

    env: baselines.common.vec_env.VecEnv     environment. Needs to be vectorized for parallel environment simulation.
                                      The environments produced by gym.make can be wrapped using baselines.common.vec_env.DummyVecEnv class.


    nsteps: int                       number of steps of the vectorized environment per update (i.e. batch size is nsteps * nenv where
                                      nenv is number of environment copies simulated in parallel). Note: in ppo.py nsteps is the "batch
                                      size" whereas the "total batch size" is nsteps * nenv

    total_timesteps: int              number of timesteps (i.e. number of actions taken in the environment)

    ent_coef: float                   policy entropy coefficient in the optimization objective

    lr: float or function             learning rate, constant or a schedule function [0,1] -> R+ where 1 is beginning of the
                                      training and 0 is the end of the training.

    vf_coef: float                    value function loss coefficient in the optimization objective

    max_grad_norm: float or None      gradient norm clipping coefficient

    gamma: float                      discounting factor

    lam: float                        advantage estimation discounting factor (lambda in the paper)

    log_interval: int                 number of timesteps between logging events

    nminibatches: int                 number of training minibatches per update. For recurrent policies,
                                      should be smaller or equal than number of environments run in parallel.

    noptepochs: int                   number of training epochs per update

    cliprange: float or function      clipping range, constant or schedule function [0,1] -> R+ where 1 is beginning of the training
                                      and 0 is the end of the training

    save_interval: int                number of timesteps between saving events

    load_path: str                    path to load the model from

    **network_kwargs:                 keyword arguments to the policy / network builder. See baselines.common/policies.py/build_policy and arguments to a particular type of network
                                      For instance, 'mlp' network architecture has arguments num_hidden and num_layers.



    '''
    additional_params = network_kwargs["network_kwargs"]
    from baselines import logger

    # set_global_seeds(seed) Micah: We deal with seeds upstream

    if "LR_ANNEALING" in additional_params.keys():
        lr_reduction_factor = additional_params["LR_ANNEALING"]
        start_lr = lr
        lr = lambda prop: (start_lr / lr_reduction_factor) + (start_lr - (start_lr / lr_reduction_factor)) * prop # Anneals linearly from lr to lr/red factor

    if isinstance(lr, float): lr = constfn(lr)
    else: assert callable(lr)
    if isinstance(cliprange, float): cliprange = constfn(cliprange)
    else: assert callable(cliprange)
    total_timesteps = int(total_timesteps)

    policy = build_policy(env, network, **network_kwargs)
    
    bestrew = -np.Inf
    # Get the nb of env
    nenvs = env.num_envs

    # Get state_space and action_space
    ob_space = env.observation_space
    ac_space = env.action_space

    # Calculate the batch_size
    nbatch = nenvs * nsteps # Micah: nbatch is the total batch size. Each env simulates nsteps
    nbatch_train = nbatch // nminibatches # Micah: the minibactch size – the agreggated batch across simulation threads is then divided into nminibatches, and gradients are computed on each of these minibatches

    # Instantiate the model object (that creates act_model and train_model)
    if model_fn is None:
        from baselines.ppo2.model import Model
        model_fn = Model

    model = model_fn(policy=policy, ob_space=ob_space, ac_space=ac_space, nbatch_act=nenvs, nbatch_train=nbatch_train,
                    nsteps=nsteps, ent_coef=ent_coef, vf_coef=vf_coef,
                    max_grad_norm=max_grad_norm, scope=scope)

    if load_path is not None:
        model.load(load_path)

    env_name = 'unknown' if 'env_name' not in env.__dict__.keys() else env.env_name
    model.env_name = env_name
    
    # Instantiate the runner object
    runner = Runner(env=env, model=model, nsteps=nsteps, gamma=gamma, lam=lam)
    if eval_env is not None:
        eval_runner = Runner(env = eval_env, model = model, nsteps = nsteps, gamma = gamma, lam= lam)

    epinfobuf = deque(maxlen=100)
    if eval_env is not None:
        eval_epinfobuf = deque(maxlen=100)

    # Start total timer
    tfirststart = time.perf_counter()

    best_rew_per_step = 0

    run_info = defaultdict(list)
    nupdates = total_timesteps // nbatch
    print("TOT NUM UPDATES", nupdates)
    for update in range(1, nupdates+1):

        print("UPDATE {} / {}; (seed: {})".format(update, nupdates, additional_params["CURR_SEED"]))

        assert nbatch % nminibatches == 0, "Have {} total batch size and want {} minibatches, can't split evenly".format(nbatch, nminibatches)
        # Start timer
        tstart = time.perf_counter()
        frac = 1.0 - (update - 1.0) / nupdates
        # Calculate the learning rate
        lrnow = lr(frac)
        # Calculate the cliprange
        cliprangenow = cliprange(frac)
        # Get minibatch
        obs, returns, masks, actions, values, neglogpacs, states, epinfos = runner.run() #pylint: disable=E0632
        
        if eval_env is not None:
            eval_obs, eval_returns, eval_masks, eval_actions, eval_values, eval_neglogpacs, eval_states, eval_epinfos = eval_runner.run() #pylint: disable=E0632

        eplenmean = safemean([epinfo['ep_length'] for epinfo in epinfos])
        eprewmean = safemean([epinfo['r'] for epinfo in epinfos])
        rew_per_step = eprewmean / eplenmean

        print("Curr learning rate {} \t Curr reward per step {}".format(lrnow, rew_per_step))

        if rew_per_step > best_rew_per_step and early_stopping:
            # Avoid updating best model at first iteration because the means might be a bit off because
            # of how the multithreaded batch simulation works
            best_rew_per_step = eprewmean / eplenmean
            checkdir = osp.join(logger.get_dir(), 'checkpoints')
            model.save(checkdir + ".temp_best_model")
            print("Saved model as best", best_rew_per_step, "avg rew/step")

        epinfobuf.extend(epinfos)
        if eval_env is not None:
            eval_epinfobuf.extend(eval_epinfos)

        # Here what we're going to do is for each minibatch calculate the loss and append it.
        mblossvals = []
        if states is None: # nonrecurrent version
            # Index of each element of batch_size
            # Create the indices array
            inds = np.arange(nbatch)
            for _ in range(noptepochs):
                # Randomize the indexes
                np.random.shuffle(inds)
                # 0 to batch_size with batch_train_size step
                for start in tqdm.trange(0, nbatch, nbatch_train, desc="{}/{}".format(_, noptepochs)):
                    end = start + nbatch_train
                    mbinds = inds[start:end]
                    slices = (arr[mbinds] for arr in (obs, returns, masks, actions, values, neglogpacs))
                    mblossvals.append(model.train(lrnow, cliprangenow, *slices))

        else: # recurrent version
            # Micah: My understanding is that the main difference lies in the randomization.
            # We don't shuffle indices anymore within-episode, but only which envs' rollouts
            # go in each minibatch
            assert nenvs % nminibatches == 0
            envsperbatch = nenvs // nminibatches
            envinds = np.arange(nenvs)
            flatinds = np.arange(nenvs * nsteps).reshape(nenvs, nsteps)
            for _ in range(noptepochs):
                np.random.shuffle(envinds)
                for start in range(0, nenvs, envsperbatch):
                    end = start + envsperbatch
                    mbenvinds = envinds[start:end]
                    mbflatinds = flatinds[mbenvinds].ravel()
                    slices = (arr[mbflatinds] for arr in (obs, returns, masks, actions, values, neglogpacs))
                    mbstates = states[mbenvinds]
                    mblossvals.append(model.train(lrnow, cliprangenow, *slices, mbstates))

        # Feedforward --> get losses --> update
        lossvals = np.mean(mblossvals, axis=0)
        # End timer
        tnow = time.perf_counter()
        # Calculate the fps (frame per second)
        fps = int(nbatch / (tnow - tstart))

        if update % log_interval == 0 or update == 1:
            # Calculates if value function is a good predicator of the returns (ev > 1)
            # or if it's just worse than predicting nothing (ev =< 0)
            ev = explained_variance(values, returns)
            logger.logkv("serial_timesteps", update*nsteps)
            logger.logkv("nupdates", update)

            timesteps_passed = update*nbatch
            logger.logkv("total_timesteps", timesteps_passed)
            run_info['total_timesteps'].append(timesteps_passed)

            logger.logkv("fps", fps)
            logger.logkv("explained_variance", float(ev))
            run_info['explained_variance'].append(float(ev))
            
            eprewmean = safemean([epinfo['r'] for epinfo in epinfobuf])
            logger.logkv('ep_perceived_rew_mean', eprewmean)
            run_info['ep_perceived_rew_mean'].append(eprewmean)

            main_agent_indices_for_info_buffers = [epinfo['policy_agent_idx'] for epinfo in epinfobuf]
            if additional_params["ENVIRONMENT_TYPE"] == "Gathering":
                # print(main_agent_indices_for_info_buffers)
                # print("GAME STATS", [epinfo['ep_game_stats'] for epinfo in epinfobuf])
                for k in epinfobuf[0]['ep_game_stats'].keys():
                    gamestat_mean = safemean([epinfo['ep_game_stats'][k][main_idx] for main_idx, epinfo in zip(main_agent_indices_for_info_buffers, epinfobuf)])
                    run_info["{}_main".format(k)].append(gamestat_mean)

                    gamestat_mean_other = safemean([epinfo['ep_game_stats'][k][1 - main_idx] for main_idx, epinfo in zip(main_agent_indices_for_info_buffers, epinfobuf)])
                    run_info["{}_other".format(k)].append(gamestat_mean_other)

                    logger.logkv("_{}_main".format(k), gamestat_mean)
                    logger.logkv("_{}_other".format(k), gamestat_mean_other)

            if additional_params["ENVIRONMENT_TYPE"] == "Overcooked":
                # Look at episode infos, find the game stats, keep track of them, and log them
                from overcooked_ai_py.mdp.overcooked_mdp import EVENT_TYPES
                for k in EVENT_TYPES:
                    gamestat_mean = safemean([len(epinfo['ep_game_stats'][k][main_idx]) for main_idx, epinfo in zip(main_agent_indices_for_info_buffers, epinfobuf)])
                    run_info["{}_main".format(k)].append(gamestat_mean)

                    gamestat_mean_other = safemean([len(epinfo['ep_game_stats'][k][1 - main_idx]) for main_idx, epinfo in zip(main_agent_indices_for_info_buffers, epinfobuf)])
                    run_info["{}_other".format(k)].append(gamestat_mean_other)

                    logger.logkv("_{}_main".format(k), gamestat_mean)
                    logger.logkv("_{}_other".format(k), gamestat_mean_other)

                # Look at episode infos and look at task contribution by agent
                for rew_type in ["sparse", "shaped"]:
                    k = 'ep_{}_r_by_agent'.format(rew_type)

                    gamestat_mean = safemean([epinfo[k][main_idx] for main_idx, epinfo in zip(main_agent_indices_for_info_buffers, epinfobuf)])
                    run_info["{}_r_main_contrib".format(rew_type)].append(gamestat_mean)
                    gamestat_mean_other = safemean([epinfo[k][1 - main_idx] for main_idx, epinfo in zip(main_agent_indices_for_info_buffers, epinfobuf)])
                    run_info["{}_r_other_contrib".format(rew_type)].append(gamestat_mean_other)

                    logger.logkv("_{}_r_contrib_main".format(rew_type), gamestat_mean)
                    logger.logkv("_{}_r_contrib_other".format(rew_type), gamestat_mean_other)


            # TODO: agent_infos (4/23/2020 ?)
            ood_proportion = safemean([epinfo['OTHER_OOD'] for epinfo in epinfobuf])
            if ood_proportion != 0.5:
                logger.logkv("_OOD_percentage_other", ood_proportion)
                run_info['ood_other'].append(ood_proportion)
            
            ep_dense_rew_mean = safemean([epinfo['ep_shaped_r'] for epinfo in epinfobuf])
            run_info['ep_dense_rew_mean'].append(ep_dense_rew_mean)

            ep_sparse_rew_mean = safemean([epinfo['ep_sparse_r'] for epinfo in epinfobuf])
            logger.logkv('ep_sparse_rew_mean', safemean([epinfo['ep_sparse_r'] for epinfo in epinfobuf]))
            run_info['ep_sparse_rew_mean'].append(ep_sparse_rew_mean)
            
            eplenmean = safemean([epinfo['ep_length'] for epinfo in epinfobuf])
            logger.logkv('eplenmean', eplenmean)
            run_info['eplenmean'].append(eplenmean)

            if eval_env is not None:
                logger.logkv('eval_eprewmean', safemean([epinfo['r'] for epinfo in eval_epinfobuf]) )
                logger.logkv('eval_eplenmean', safemean([epinfo['l'] for epinfo in eval_epinfobuf]) )
            
            time_elapsed = tnow - tfirststart
            logger.logkv('time_elapsed', time_elapsed)

            time_per_update = time_elapsed / update
            time_remaining = (nupdates - update) * time_per_update
            logger.logkv('time_remaining', time_remaining / 60)
            
            for (lossval, lossname) in zip(lossvals, model.loss_names):
                run_info[lossname].append(lossval)
                
                logger.logkv(lossname, lossval)

            if MPI is None or MPI.COMM_WORLD.Get_rank() == 0:
                logger.dumpkvs()

            # For TOM, every EVAL_FREQ updates we evaluate the agent with TOMs and BCs
            if additional_params["OTHER_AGENT_TYPE"]  == "tom" \
                    and update % additional_params["EVAL_FREQ"] == 0:
                run_info = env.other_agent[0].eval_and_viz_tom(additional_params, env, model, run_info)

            # Update current logs
            if additional_params["RUN_TYPE"] in ["ppo", "joint_ppo"]:
                from overcooked_ai_py.utils import save_dict_to_file
                save_dict_to_file(run_info, additional_params["CURRENT_SEED_DIR"] + "temp_logs")

                if additional_params["TRACK_TUNE"]:
                    from ray import tune
                    tune.track.log(
                        sparse_reward=ep_sparse_rew_mean, 
                        dense_reward=ep_dense_rew_mean, 
                        timesteps_total=timesteps_passed
                    )

                # Linear annealing of reward shaping
                if additional_params["REW_SHAPING_HORIZON"] != 0:
                    # Piecewise linear annealing schedule
                    # annealing_thresh: until when we should stop doing 100% reward shaping
                    # annealing_horizon: when we should reach doing 0% reward shaping
                    annealing_horizon = additional_params["REW_SHAPING_HORIZON"]
                    annealing_thresh = 0

                    def fn(x):
                        if annealing_thresh != 0 and annealing_thresh - (annealing_horizon / annealing_thresh) * x > 1:
                            return 1
                        else:
                            fn = lambda x: -1 * (x - annealing_thresh) * 1 / (annealing_horizon - annealing_thresh) + 1
                            return max(fn(x), 0)

                    curr_timestep = update * nbatch
                    curr_reward_shaping = fn(curr_timestep)
                    env.update_reward_shaping_param(curr_reward_shaping)
                    print("Current reward shaping", curr_reward_shaping)

                sp_horizon = additional_params["SELF_PLAY_HORIZON"]

                # Save/overwrite best model
                if ep_sparse_rew_mean > bestrew:
                    # Don't save best model if still doing some self play and it's supposed to be a BC model
                    if additional_params["OTHER_AGENT_TYPE"][:2] == "bc" and sp_horizon != 0 and env.self_play_randomization > 0:
                        pass
                    else:    
                        from human_aware_rl.ppo.ppo import save_ppo_model
                        print("BEST REW", ep_sparse_rew_mean, "overwriting previous model with", bestrew)
                        save_ppo_model(model, "{}seed{}/best".format(
                            additional_params["SAVE_DIR"],
                            additional_params["CURR_SEED"]), 
                            additional_params
                        )
                        bestrew = max(ep_sparse_rew_mean, bestrew)

                # If not sp run, and horizon is not None, 
                # vary amount of self play over time, either with a sigmoidal feedback loop 
                # or with a fixed piecewise linear schedule.
                if additional_params["OTHER_AGENT_TYPE"] != "sp" and sp_horizon is not None:
                    if type(sp_horizon) is not list:
                        # Sigmoid self-play schedule based on current performance (not recommended)
                        curr_reward = ep_sparse_rew_mean

                        rew_target = sp_horizon
                        shift = rew_target / 2
                        t = (1 / rew_target) * 10
                        fn = lambda x: -1 * (np.exp(t * (x - shift)) / (1 + np.exp(t * (x - shift)))) + 1
                        
                        env.self_play_randomization = fn(curr_reward)
                        print("Current self-play randomization", env.self_play_randomization)
                    else:
                        assert len(sp_horizon) == 2
                        # Piecewise linear self-play schedule

                        # self_play_thresh: when we should stop doing 100% self-play
                        # self_play_timeline: when we should reach doing 0% self-play
                        self_play_thresh, self_play_timeline = sp_horizon

                        def fn(x):
                            if self_play_thresh != 0 and self_play_timeline - (self_play_timeline / self_play_thresh) * x > 1:
                                return 1
                            else:
                                fn = lambda x: -1 * (x - self_play_thresh) * 1 / (self_play_timeline - self_play_thresh) + 1
                                return max(fn(x), 0)

                        curr_timestep = update * nbatch
                        env.self_play_randomization = fn(curr_timestep)
                        print("Current self-play randomization", env.self_play_randomization)



        if save_interval and (update % save_interval == 0 or update == 1) and logger.get_dir() and (MPI is None or MPI.COMM_WORLD.Get_rank() == 0):
            checkdir = osp.join(logger.get_dir(), 'checkpoints')
            os.makedirs(checkdir, exist_ok=True)
            savepath = osp.join(checkdir, '%.5i'%update)
            print('Saving to', savepath)
            model.save(savepath)

        # Visualization of rollouts with actual other agent
        run_type = additional_params["RUN_TYPE"]
        if run_type in ["ppo", "joint_ppo"] and update % additional_params["VIZ_FREQUENCY"] == 0:

            from human_aware_rl.ppo.ppo import PPOAgent
            
            if env_name == "Overcooked-v0":
                from overcooked_ai_py.mdp.overcooked_env import OvercookedEnv
                from overcooked_ai_py.mdp.overcooked_mdp import OvercookedGridworld
                from overcooked_ai_py.mdp.layout_generator import LayoutGenerator
                from overcooked_ai_py.agents.agent import AgentPair

                mdp_params = additional_params["mdp_params"]
                env_params = additional_params["env_params"]
                mdp_gen_params = additional_params["mdp_generation_params"]
                mdp_fn = LayoutGenerator.mdp_gen_fn_from_dict(mdp_params=mdp_params, **mdp_gen_params)
                base_env = OvercookedEnv(mdp_fn, **env_params)
            elif env_name == "Gathering-v0":
                from gathering_ai_py.mdp.gathering_env import GatheringEnv
                from gathering_ai_py.mdp.gathering_mdp import GatheringGridworld
                from gathering_ai_py.agents.agent import AgentPair

                mdp_params = additional_params["mdp_params"]
                env_params = additional_params["env_params"]
                mdp_fn = lambda: GatheringGridworld.from_layout_name(**mdp_params)
                base_env = GatheringEnv(mdp=mdp_fn, **env_params)
            else:
                raise ValueError("Unrecognized Env")

            print(additional_params["SAVE_DIR"])

            display_until = 100
            agent = PPOAgent.from_model(model, additional_params)
            agent.set_mdp(base_env.mdp)

            if not additional_params["OTHER_AGENT_TYPE"] == 'tom':  # For TOM we vizualise
                # and also evaluate the performance of the ppo with various agents in section "if update % log_interval"

                if run_type == "ppo":
                    if additional_params["OTHER_AGENT_TYPE"] == 'sp':
                        agent_pair = AgentPair(agent, agent, allow_duplicate_agents=True)

                    else:
                        print("PPO agent on index 0:")
                        env.other_agent.set_mdp(base_env.mdp)
                        agent_pair = AgentPair(agent, env.other_agent)
                        trajectory, time_taken, tot_rewards, _ = base_env.run_agents(agent_pair, display=True, display_until=100)
                        base_env.reset()
                        agent_pair.reset()
                        print("Tot rew", tot_rewards)

                        print("PPO agent on index 1:")
                        agent_pair = AgentPair(env.other_agent, agent)

                else:
                    agent_pair = AgentPair(agent)

                trajectory, time_taken, tot_rewards, _ = base_env.run_agents(agent_pair, display=True, display_until=100)
                base_env.reset()
                agent_pair.reset()
                print("tot rew", tot_rewards)

            print(additional_params["SAVE_DIR"])

        # num_entropy_iter = nupdates // 10
        # if update % num_entropy_iter == 0 or update == nupdates - 1:
        #     mdp_params = additional_params["mdp_params"]
        #     env_params = additional_params["env_params"]
        #     ae = AgentEvaluator(mdp_params, env_params)
        #     _ = ae.evaluate_agent_pair(agent_pair, num_games=100)
        #     entropies = AgentEvaluator.trajectory_entropy(_)
        #     run_info["policy_entropy"].append(entropies)
        #     avg_rew_and_se = AgentEvaluator.trajectory_mean_and_se_rewards(_)
        #     run_info["policy_reward"].append(avg_rew_and_se[0])

    if nupdates > 0 and early_stopping:
        checkdir = osp.join(logger.get_dir(), 'checkpoints')
        print("Loaded best model", best_rew_per_step)
        model.load(checkdir + ".temp_best_model")
    return model, run_info
# Avoid division error when calculate the mean (in our case if epinfo is empty returns np.nan, not return an error)
def safemean(xs):
    return np.nan if len(xs) == 0 else np.mean(xs)

