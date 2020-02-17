import numpy as np
import copy
from baselines.common.runners import AbstractEnvRunner

class Runner(AbstractEnvRunner):
    """
    We use this object to make a mini batch of experiences
    __init__:
    - Initialize the runner

    run():
    - Make a mini batch
    """
    def __init__(self, *, env, model, nsteps, gamma, lam):
        super().__init__(env=env, model=model, nsteps=nsteps)
        # Lambda used in GAE (General Advantage Estimation)
        self.lam = lam
        # Discount rate
        self.gamma = gamma

    def run(self):
        # Here, we init the lists that will contain the mb of experiences
        mb_obs, mb_rewards, mb_actions, mb_values, mb_dones, mb_neglogpacs = [],[],[],[],[],[]
        mb_states = self.states
        epinfos = []
        # For n in range number of steps

        import time
        tot_time = time.time()
        int_time = 0
        num_envs = len(self.curr_state)

        if self.env.trajectory_sp:
            # Selecting which environments should run fully in self play
            sp_envs_bools = np.random.random(num_envs) < self.env.self_play_randomization
            print("SP envs: {}/{}".format(sum(sp_envs_bools), num_envs))

        # For TOM/BC agents, set the personality params / choose the BC for each parallel agent for this trajectory
        if self.env.run_type is "ppo" and self.env.other_agent_type in ["bc_pop", "tom"]:
            other_agent_choices = []
            for i in range(self.env.num_envs):
                if self.env.other_agent_type == "tom":
                    tom_params_choice = self.env.other_agent[i].set_tom_params(self.env.num_toms,
                                                                               self.other_agent_idx[i], self.env.tom_params)
                    other_agent_choices.append(tom_params_choice)
                elif self.env.other_agent_type == "bc_pop":
                    # Randomly select a BC agent from the "store" to set as the i'th other_agent:
                    bc_to_choose = np.random.randint(0, self.env.bc_pop_size)
                    #TODO: Might be a better way of doing this, rather than making a deep copy?
                    self.env.other_agent[i] = copy.deepcopy(self.env.bc_agent_store[bc_to_choose])
                    self.env.other_agent[i].set_agent_index(self.other_agent_idx[i])
                    self.env.other_agent[i].reset()
                    other_agent_choices.append(bc_to_choose)

            print('The TOM/BC agents selected in each env are: {}'.format(other_agent_choices))

        other_agent_simulation_time = 0

        from overcooked_ai_py.mdp.actions import Action

        def get_other_agent_actions():

            # if self.env.use_action_method:
            #     other_agent_actions = self.env.other_agent.actions(self.curr_state, self.other_agent_idx)
            #     actions, probs = zip(*other_agent_actions)
            #     return [Action.ACTION_TO_INDEX[a] for a in actions]

            if self.env.other_agent_type in ["bc_pop", "tom"]:

                # We have SIM_THREADS parallel other_agents. The i'th takes curr_state[i], and returns an action
                other_agent_actions = []
                for i in range(len(self.other_agent_idx)):

                    #TODO: This is needed because if batch size = n*horizon for n>1, then after one epsiode the index
                    # might be switched. Adding this here is just a quick fix, and should be fixed at the source (i.e. when the index changes)
                    if self.env.other_agent[i].agent_index != self.other_agent_idx[i]:
                        self.env.other_agent[i].set_agent_index(self.other_agent_idx[i])

                    action, _ = self.env.other_agent[i].action(self.curr_state[i])
                    other_agent_actions.append(action)

                return [Action.ACTION_TO_INDEX[a] for a in other_agent_actions]

            else:   #TODO: This shouldn't be "else", because this won't work for all agent types
                actions, _ = self.env.other_agent.direct_policy(self.obs1)
                return actions


        for _ in range(self.nsteps):
            # Given observations, get action value and neglopacs
            # We already have self.obs because Runner superclass run self.obs[:] = env.reset() on init
            overcooked = 'env_name' in self.env.__dict__.keys() and self.env.env_name == "Overcooked-v0"
            if overcooked:

                actions, values, self.states, neglogpacs = self.model.step(self.obs0, S=self.states, M=self.dones)

                import time
                current_simulation_time = time.time()

                # Randomize at either the trajectory level or the individual timestep level
                if self.env.trajectory_sp:

                    # If there are environments selected to not run in SP, generate actions
                    # for the other agent, otherwise we skip this step.
                    #TODO: It's (slightly) inefficient to calculate all actions, even though only 1 might be used
                    if sum(sp_envs_bools) != num_envs:
                        other_agent_actions_non_sp = get_other_agent_actions()

                    # If there are environments selected to run in SP, generate self-play actions
                    if sum(sp_envs_bools) != 0:
                        other_agent_actions_sp, _, _, _ = self.model.step(self.obs1, S=self.states, M=self.dones)

                    # Select other agent actions for each environment depending on whether it was selected
                    # for self play or not
                    other_agent_actions = []
                    for i in range(num_envs):
                        if sp_envs_bools[i]:
                            other_agent_actions.append(other_agent_actions_sp[i])
                        else:
                            other_agent_actions.append(other_agent_actions_non_sp[i])
                
                else:
                    other_agent_actions = np.zeros_like(self.curr_state)

                    if self.env.self_play_randomization < 1:
                        # Get actions through the action method of the agent
                        other_agent_actions = get_other_agent_actions()

                    # Naive non-parallelized way of getting actions for other
                    if self.env.self_play_randomization > 0:
                        self_play_actions, _, _, _ = self.model.step(self.obs1, S=self.states, M=self.dones)
                        self_play_bools = np.random.random(num_envs) < self.env.self_play_randomization

                        for i in range(num_envs):
                            is_self_play_action = self_play_bools[i]
                            if is_self_play_action:
                                other_agent_actions[i] = self_play_actions[i]

                # NOTE: This has been discontinued as now using .other_agent_true takes about the same amount of time
                # elif self.env.other_agent_bc:
                #     # Parallelise actions with direct action, using the featurization function
                #     featurized_states = [self.env.mdp.featurize_state(s, self.env.mlp) for s in self.curr_state]
                #     player_featurizes_states = [s[idx] for s, idx in zip(featurized_states, self.other_agent_idx)]
                #     other_agent_actions = self.env.other_agent.direct_policy(player_featurizes_states, sampled=True, no_wait=True)

                other_agent_simulation_time += time.time() - current_simulation_time

                joint_action = [(actions[i], other_agent_actions[i]) for i in range(len(actions))]

                mb_obs.append(self.obs0.copy())
            else:
                actions, values, self.states, neglogpacs = self.model.step(self.obs, S=self.states, M=self.dones)
                mb_obs.append(self.obs.copy())

            mb_actions.append(actions)
            mb_values.append(values)
            mb_neglogpacs.append(neglogpacs)
            mb_dones.append(self.dones)

            # Take actions in env and look the results
            # Infos contains a ton of useful informations
            if overcooked:
                obs, rewards, self.dones, infos = self.env.step(joint_action)
                # print("REWS", rewards, np.mean(rewards))
                both_obs = obs["both_agent_obs"]
                self.obs0[:] = both_obs[:, 0, :, :]
                self.obs1[:] = both_obs[:, 1, :, :]
                self.curr_state = obs["overcooked_state"]
                self.other_agent_idx = obs["other_agent_env_idx"]

                # TODO: Quick fix: if self.dones then we're at the end of an episode, so the agent's history might need to be reset
                if self.dones[i]:
                    self.env.other_agent[i].reset()

            else:
                self.obs[:], rewards, self.dones, infos = self.env.step(actions)

            for info in infos:
                maybeepinfo = info.get('episode')
                if maybeepinfo: epinfos.append(maybeepinfo)
            mb_rewards.append(rewards)

        print("Other agent actions took", other_agent_simulation_time, "seconds")
        tot_time = time.time() - tot_time
        print("Total simulation time for {} steps: {} \t Other agent action time: {} \t {} steps/s".format(self.nsteps, tot_time, int_time, self.nsteps / tot_time))
        
        #batch of steps to batch of rollouts
        mb_obs = np.asarray(mb_obs, dtype=self.obs.dtype)
        mb_rewards = np.asarray(mb_rewards, dtype=np.float32)
        mb_actions = np.asarray(mb_actions)
        mb_values = np.asarray(mb_values, dtype=np.float32)
        mb_neglogpacs = np.asarray(mb_neglogpacs, dtype=np.float32)
        mb_dones = np.asarray(mb_dones, dtype=np.bool)
        last_values = self.model.value(self.obs, S=self.states, M=self.dones)

        # discount/bootstrap off value fn
        mb_returns = np.zeros_like(mb_rewards)
        mb_advs = np.zeros_like(mb_rewards)
        lastgaelam = 0
        for t in reversed(range(self.nsteps)):
            if t == self.nsteps - 1:
                nextnonterminal = 1.0 - self.dones
                nextvalues = last_values
            else:
                nextnonterminal = 1.0 - mb_dones[t+1]
                nextvalues = mb_values[t+1]
            delta = mb_rewards[t] + self.gamma * nextvalues * nextnonterminal - mb_values[t]
            mb_advs[t] = lastgaelam = delta + self.gamma * self.lam * nextnonterminal * lastgaelam
        mb_returns = mb_advs + mb_values
        return (*map(sf01, (mb_obs, mb_returns, mb_dones, mb_actions, mb_values, mb_neglogpacs)),
            mb_states, epinfos)

# obs, returns, masks, actions, values, neglogpacs, states = runner.run()
def sf01(arr):
    """
    swap and then flatten axes 0 and 1
    """
    s = arr.shape
    return arr.swapaxes(0, 1).reshape(s[0] * s[1], *s[2:])
