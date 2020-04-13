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

        #TODO: Consider putting this back in: it creates a reduced pop so that each call to the bc is for more states
        # Reduce the BC pop size for this iteration of runner (so that there are less calls to BCs during runner):
        # if self.env.other_agent_type == "bc_pop":
        #     reduced_bc_pop_size = np.int(self.env.reduced_bc_pop_fraction*`self.env.bc_pop_s`ize)
        #     bc_reduced_pop = np.sort(np.random.choice(np.arange(self.env.bc_pop_size), size=reduced_bc_pop_size,
        #                                               replace=False))

        # For TOM/BC agents, set the personality params / choose the BC for each parallel agent for this trajectory
        if self.env.run_type is "ppo" and self.env.other_agent_type in ["bc_pop", "tom", "tom_bc"]:
            other_agent_choices = []
            tom_this_env = [0]*self.env.num_envs
            for i in range(self.env.num_envs):
                if sp_envs_bools[i]:
                    other_agent_choices.append('SP')
                else:
                    if self.env.other_agent_type == "tom":
                        tom_params_choice = self.env.other_agent[i].set_tom_params(self.env.num_toms,
                                                                        self.other_agent_idx[i], self.env.tom_params)
                        other_agent_choices.append(tom_params_choice)
                    elif self.env.other_agent_type == "bc_pop":

                        #TODO: Link this together with the 1-bc code below
                        if self.env.bc_pop_size > 1:

                            # TODO: Consider putting this back in: it creates a reduced pop so that each call to the bc is for more states:
                            # bc_choice = np.random.choice(bc_reduced_pop)

                            bc_choice = np.random.randint(0, self.env.bc_pop_size)
                            if bc_choice not in other_agent_choices:
                                self.env.other_agent[i] = self.env.bc_agent_store[bc_choice]
                                self.env.other_agent[i].reset()
                                #TODO: Remove these (they are included below now)
                                # self.env.other_agent[i].states_for_bc = []
                                # self.env.other_agent[i].player_idx_for_bc = []
                                # self.env.other_agent[i].parallel_idx_for_bc = []
                            else:
                                pass
                            other_agent_choices.append(bc_choice)
                        else:
                            other_agent_choices.append("BC0")

                    elif self.env.other_agent_type == "tom_bc":
                        # We assume that we're splitting the agents into 1/2 bc and 1/2 TOM, so make the first 1/2 of
                        # gym.envs as BCs, and the rest TOMs. If there is only one BC, then make it a parallel BC agent (above)
                        if i < self.env.num_envs/2 and self.env.bc_pop_size == 1:
                            other_agent_choices.append("BC0")
                        elif i < self.env.num_envs/2 and self.env.bc_pop_size > 1:
                            # For the first half of envs, make BCs (if there's more than one BC):
                            bc_to_choose = np.random.randint(0, self.env.bc_pop_size)
                            # If we've already taken this BC from the store, then make a deepcopy:
                            # TODO: Neglecting the fact that a TOM could be chosen with the same number! Wasted deepcopy sometimes:
                            if bc_to_choose in other_agent_choices:
                                self.env.other_agent[i] = copy.deepcopy(self.env.bc_agent_store[bc_to_choose])
                            else:  # Otherwise it's fine to use the agent in the store directly
                                self.env.other_agent[i] = self.env.bc_agent_store[bc_to_choose]
                            self.env.other_agent[i].set_agent_index(self.other_agent_idx[i])
                            self.env.other_agent[i].reset()
                            other_agent_choices.append(bc_to_choose)
                        elif i >= self.env.num_envs/2:
                            # Make a TOM agent:
                            from human_aware_rl.ppo.ppo_pop import make_tom_agent
                            self.env.other_agent[i] = make_tom_agent(self.env.mlp)
                            tom_params_choice = self.env.other_agent[i].set_tom_params(self.env.num_toms,
                                                                                       self.other_agent_idx[i],
                                                                                       self.env.tom_params)
                            tom_this_env[i] = 1
                            other_agent_choices.append(tom_params_choice)

            #TODO: Special case: bc with pop_size=1. This should be extended to pop_size>1 and incorporated into above
            if self.env.other_agent_type in ["bc_pop", "tom_bc"] and self.env.bc_pop_size == 1:
                self.env.other_agent[0] = self.env.bc_agent_store[0]
                self.env.other_agent[0].reset
                self.env.other_agent[0].parallel_indices = list(range(self.env.num_envs)) \
                    if self.env.other_agent_type == "bc_pop" else list(range(self.env.num_envs // 2))  # If tom_bc then only the first half of envs will have the bc

            print('The TOM/BC agents selected in each env are: {}'.format(other_agent_choices))

        other_agent_simulation_time = 0

        from overcooked_ai_py.mdp.actions import Action

        def get_other_agent_actions(sp_envs_bools):
            """Get actions for the other agent. If the agent is BC or TOM, then only get actions for envs that aren't being used for self-play"""

            other_agent_actions = []

            #TODO: Special case: bc with pop_size=1. This should be extended to pop_size>1 and incorporated into below
            if self.env.other_agent_type in ["bc_pop", "tom_bc"] and self.env.bc_pop_size == 1:

                # Find only the states (& indices) for the envs with BCs in:
                states_for_bc, player_idx_for_bc, parallel_idx_for_bc = [], [], []
                for i in range(self.env.num_envs):
                    if not (sp_envs_bools[i] or tom_this_env[i]):
                        states_for_bc.append(self.curr_state[i])
                        player_idx_for_bc.append(self.other_agent_idx[i])
                        parallel_idx_for_bc.append(self.env.other_agent[0].parallel_indices[i])

                # Get all actions for the reduced list of states:
                bc_actions_and_probs = self.env.other_agent[0].actions(states_for_bc, player_idx_for_bc, parallel_idx_for_bc)
                bc_action_indices = [Action.ACTION_TO_INDEX[bc_actions_and_probs[i][0]] for i in range(len(states_for_bc))]

                for i in range(self.env.num_envs):
                    if sp_envs_bools[i]:
                        other_agent_actions.append(None)
                    elif tom_this_env[i]:
                        # TOM actions:
                        if self.env.other_agent[i].agent_index != self.other_agent_idx[i]:
                            self.env.other_agent[i].set_agent_index(self.other_agent_idx[i])
                        tom_action, _ = self.env.other_agent[i].action(self.curr_state[i])
                        tom_action_index = Action.ACTION_TO_INDEX[tom_action]
                        other_agent_actions.append(tom_action_index)
                    else:
                        # Append others actions with the first action from the action_indices list
                        other_agent_actions.append(bc_action_indices.pop(0))
                return other_agent_actions

            #TODO: MERGE WITH 1 BC ABOVE!
            elif self.env.other_agent_type == "bc_pop" and self.env.bc_pop_size > 1:

                # Set up the BCs so that they can be called with several states in parallel:
                bcs_to_call = []
                for i in range(self.env.num_envs):
                    if not sp_envs_bools[i]:
                        bc_choice = other_agent_choices[i]
                        if bc_choice not in bcs_to_call:
                            bcs_to_call.append(bc_choice)
                            self.env.other_agent[i].states_for_bc=[self.curr_state[i]]
                            self.env.other_agent[i].player_idx_for_bc=[self.other_agent_idx[i]]
                            self.env.other_agent[i].parallel_idx_for_bc=[i]
                        else:
                            # BC has already been allocated a parallel env:
                            bcs_to_call.append(None)
                            bc_env_idx = other_agent_choices.index(bc_choice)  # This identifies the parallel env index for this agent
                            self.env.other_agent[bc_env_idx].states_for_bc.append(self.curr_state[i])
                            self.env.other_agent[bc_env_idx].player_idx_for_bc.append(self.other_agent_idx[i])
                            self.env.other_agent[bc_env_idx].parallel_idx_for_bc.append(i)

                all_action_indices = []
                # Now find the actions:
                for i in range(len(bcs_to_call)):
                    if bcs_to_call[i] is not None:
                        bc_env_idx = other_agent_choices.index(bcs_to_call[i])
                        actions_and_probs = self.env.other_agent[bc_env_idx].actions(
                                                                self.env.other_agent[bc_env_idx].states_for_bc,
                                                                self.env.other_agent[bc_env_idx].player_idx_for_bc,
                                                                self.env.other_agent[bc_env_idx].parallel_idx_for_bc)
                        action_indices = [Action.ACTION_TO_INDEX[actions_and_probs[i][0]] for i in
                                                        range(len(self.env.other_agent[bc_env_idx].states_for_bc))]
                        all_action_indices.append(action_indices)

                all_action_indices_marker = [i for i in bcs_to_call if i != None]  # This says which bcs correspond to which all_action_indices entry
                # Now fill other_agent_actions with all the actions, in the correct order:
                for i in range(self.env.num_envs):
                    if sp_envs_bools[i]:
                        other_agent_actions.append(None)
                    else:
                        bc_idx = other_agent_choices[i]
                        idx_of_this_bcs_actions = all_action_indices_marker.index(bc_idx)
                        # Append others actions with the first action from the corresponding action_indices list
                        other_agent_actions.append(all_action_indices[idx_of_this_bcs_actions].pop(0))
                return other_agent_actions


                # Find only the states (& indices) for the envs with BCs in:
                states_for_bc, player_idx_for_bc, parallel_idx_for_bc = [], [], []
                for i in range(self.env.num_envs):
                    if not sp_envs_bools[i]:
                        states_for_bc.append(self.curr_state[i])
                        player_idx_for_bc.append(self.other_agent_idx[i])
                        parallel_idx_for_bc.append(self.env.other_agent[0].parallel_indices[i])

                # Get all actions for the reduced list of states:
                actions_and_probs = self.env.other_agent[0].actions(states_for_bc, player_idx_for_bc, parallel_idx_for_bc)
                action_indices = [Action.ACTION_TO_INDEX[actions_and_probs[i][0]] for i in range(len(states_for_bc))]

                for i in range(self.env.num_envs):
                    if sp_envs_bools[i]:
                        other_agent_actions.append(None)
                    else:
                        # Append others actions with the first action from the action_indices list
                        other_agent_actions.append(action_indices.pop(0))
                return other_agent_actions

            #TODO: REMOVE BC FROM THIS:
            elif self.env.other_agent_type in ["bc_pop", "tom", "tom_bc"]:

                # We have SIM_THREADS parallel other_agents. The i'th takes curr_state[i], and returns an action
                for i in range(self.env.num_envs):

                    # Only get actions for envs that aren't being used for self-play
                    if sp_envs_bools[i]:
                        other_agent_actions.append(None)
                    else:
                        #TODO: This is needed because if batch size = n*horizon for n>1, then after one epsiode the index
                        # might be switched. Adding this here is just a quick fix, and should be fixed at the source (i.e. when the index changes). (Also change this above for tom_bc pop)
                        if self.env.other_agent[i].agent_index != self.other_agent_idx[i]:
                            self.env.other_agent[i].set_agent_index(self.other_agent_idx[i])

                        action, _ = self.env.other_agent[i].action(self.curr_state[i])
                        action_index = Action.ACTION_TO_INDEX[action]
                        other_agent_actions.append(action_index)

                return other_agent_actions

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
                    if sum(sp_envs_bools) != num_envs:
                        other_agent_actions_non_sp = get_other_agent_actions(sp_envs_bools)

                        # Check:
                        for i in range(num_envs):
                            if other_agent_actions_non_sp[i] == None:
                                assert other_agent_choices[i] == "SP", "ERROR!"

                    # If there are environments selected to run in SP, generate self-play actions
                    #TODO: This could be more efficient as we only need actions for the action SP parallel envs. E.g. select the SP envs from self.obs1, making a new obs of only these envs ("partial_obs"), then send
                    # partial_obs into self.model.step, then finally put the resulting actions in their respective parallel envs
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
                            assert other_agent_actions_non_sp[i] != None, "This action should never be None"

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

                # TODO: Quick fix: if self.dones then we're at the end of an episode, so the agent's history might need to be reset:
                for i in range(num_envs):
                    if self.dones[i] and not sp_envs_bools[i]:
                        #TODO: Merge 1-bc with >1-bc:
                        if self.env.other_agent_type == "bc_pop" and self.env.bc_pop_size == 1:
                            self.env.other_agent[0].reset()
                        else:  # When using BCs, some envs can have a None agent:
                            if self.env.other_agent[i] is not None:
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
