from ray import tune
from ray.rllib.agents.a3c.a3c_tf_policy import A3CTFPolicy
from ray.rllib.agents.a3c.a3c_torch_policy import A3CTorchPolicy
from ray.rllib.agents.a3c.a2c import A2CTrainer
from ray.rllib.agents.a3c.a3c import DEFAULT_CONFIG as A3C_CONFIG
from ray.tune.utils import merge_dicts
from ray.rllib.agents.a3c.a2c import A2C_DEFAULT_CONFIG as A2C_CONFIG
from LBF.util.vda2c_tools import *
from LBF.util.maa2c_tools import *

def run_vda2c_sum_mix(args, common_config, env_config, map_name, stop):
    if not args.force_coop:
        print("competitive settings are not suitable for VDA2C")
        raise ValueError()

    config = {
        "model": {
            "custom_model": "{}_ValueMixer".format(args.neural_arch),
            "custom_model_config": {
                "n_agents": env_config["num_agents"],
                "mixer": "qmix" if args.run == "MIX-VDA2C" else "vdn",
                "mixer_emb_dim": 64,
            },
        },
    }
    config.update(common_config)

    VDA2C_CONFIG = merge_dicts(
        A2C_CONFIG,
        {
            "agent_num": env_config["num_agents"],
        }
    )

    VDA2CTFPolicy = A3CTFPolicy.with_updates(
        name="VDA2CTFPolicy",
        postprocess_fn=value_mix_centralized_critic_postprocessing,
        loss_fn=value_mix_actor_critic_loss,
        grad_stats_fn=central_vf_stats_a2c, )

    VDA2CTorchPolicy = A3CTorchPolicy.with_updates(
        name="VDA2CTorchPolicy",
        get_default_config=lambda: VDA2C_CONFIG,
        postprocess_fn=value_mix_centralized_critic_postprocessing,
        loss_fn=value_mix_actor_critic_loss,
        mixins=[ValueNetworkMixin, MixingValueMixin],
    )

    def get_policy_class(config_):
        if config_["framework"] == "torch":
            return VDA2CTorchPolicy

    VDA2CTrainer = A2CTrainer.with_updates(
        name="VDA2CTrainer",
        default_policy=VDA2CTFPolicy,
        get_policy_class=get_policy_class,
    )

    results = tune.run(VDA2CTrainer, name=args.run + "_" + args.neural_arch + "_" + map_name, stop=stop,
                       config=config, verbose=1)

    return results

