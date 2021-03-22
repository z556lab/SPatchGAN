import os
import tensorflow as tf
from tensorflow.contrib.data import prefetch_to_device, shuffle_and_repeat, map_and_batch
from utils import get_img_paths, summary_by_keywords
from ops import l1_loss, adv_loss, regularization_loss
from imagedata import ImageData
from discriminator.discriminator_spatch import DiscriminatorSPatch
from generator.generator_basic_res import GeneratorBasicRes


class SPatchGAN:
    def __init__(self, sess, args):
        # General
        self.model_name = 'SPatchGAN'
        self.sess = sess
        self.saver = None
        self.phase = args.phase
        self.dataset_name = args.dataset
        self.test_dataset_name = args.test_dataset or args.dataset
        self.suffix = args.suffix

        # Training
        self.n_steps = args.n_steps
        self.n_iters_per_step = args.n_iters_per_step
        self.batch_size = args.batch_size
        self.img_save_freq = args.img_save_freq
        self.ckpt_save_freq = args.ckpt_save_freq
        self.summary_freq = args.summary_freq
        self.decay_step = args.decay_step
        self.init_lr = args.lr
        self.adv_weight = args.adv_weight
        self.reg_weight = args.reg_weight
        self.cyc_weight = args.cyc_weight
        self.id_weight = args.id_weight
        self.gan_type = args.gan_type

        # Input
        self.img_size = args.img_size
        self.augment_flag = args.augment_flag
        trainA_dir = os.path.join(os.path.dirname(__file__), 'dataset', self.dataset_name, 'trainA')
        trainB_dir = os.path.join(os.path.dirname(__file__), 'dataset', self.dataset_name, 'trainB')
        self.trainA_dataset = get_img_paths(trainA_dir)
        self.trainB_dataset = get_img_paths(trainB_dir)
        # Auto detect the 2nd level if there is no image at the 1st level.
        if len(self.trainA_dataset) == 0 or len(self.trainB_dataset) == 0:
            self.trainA_dataset = get_img_paths(trainA_dir, level=2)
            self.trainB_dataset = get_img_paths(trainB_dir, level=2)
        self.dataset_num = max(len(self.trainA_dataset), len(self.trainB_dataset))

        # Discriminator
        if args.dis_type == 'spatch':
            stats = []
            if args.mean_dis:
                stats.append('mean')
            if args.max_dis:
                stats.append('max')
            if args.mean_dis:
                stats.append('stddev')
            self.dis = DiscriminatorSPatch(ch=args.ch_dis,
                                           n_downsample_init=args.n_downsample_init,
                                           n_scales=args.n_scales,
                                           n_adapt=args.n_adapt,
                                           n_mix=args.n_mix,
                                           logits_type=args.logits_type_dis,
                                           stats=args.stats,
                                           sn=args.sn)
        else:
            raise ValueError('Invalid dis_type!')

        # Generator
        if args.gen_type == 'basic_res':
            self.gen = GeneratorBasicRes(ch=args.ch_gen,
                                         n_updownsample=args.n_updownsample_gen,
                                         n_res=args.n_res_gen,
                                         n_enhanced_upsample=args.n_enhanced_upsample_gen,
                                         n_mix_upsample=args.n_mix_upsample_gen,
                                         block_type=args.block_type_gen,
                                         upsample_type=args.upsample_type_gen)
            self.gen_bw = GeneratorBasicRes(ch=args.ch_gen_bw,
                                            n_updownsample=args.n_updownsample_gen_bw,
                                            n_res=args.n_res_gen_bw,
                                            n_enhanced_upsample=args.n_enhanced_upsample_gen,
                                            n_mix_upsample=args.n_mix_upsample_gen,
                                            block_type=args.block_type_gen,
                                            upsample_type=args.upsample_type_gen)
        else:
            raise ValueError('Invalid gen_type!')
        self.resolution_bw = self.img_size // args.resize_factor_gen_bw

        # Directory
        self.output_dir = args.output_dir
        self.model_dir = "{}_{}_{}".format(self.model_name, self.dataset_name, self.suffix)
        self.checkpoint_dir = os.path.join(self.output_dir, self.model_dir, args.checkpoint_dir)
        self.sample_dir = os.path.join(self.output_dir, self.model_dir, args.sample_dir)
        self.log_dir = os.path.join(self.output_dir, self.model_dir, args.log_dir)
        self.result_dir = os.path.join(self.output_dir, self.model_dir, args.result_dir)
        for dir in [self.checkpoint_dir, self.sample_dir, self.log_dir, self.result_dir]:
            os.makedirs(dir, exist_ok=True)

        print()
        print('##### Information #####')
        print('Number of trainA/B images: {}/{}'.format(len(self.trainA_dataset), len(self.trainB_dataset)) )
        print()

    def fetch_data(self, dataset):
        gpu_device = '/gpu:0'
        Image_Data_Class = ImageData(self.img_size, self.augment_flag)
        train_dataset = tf.data.Dataset.from_tensor_slices(dataset)
        train_dataset = train_dataset.apply(shuffle_and_repeat(self.dataset_num)) \
            .apply(map_and_batch(Image_Data_Class.image_processing, self.batch_size,
                                 num_parallel_batches=16, drop_remainder=True)) \
            .apply(prefetch_to_device(gpu_device, None))
        train_iterator = train_dataset.make_one_shot_iterator()
        return train_iterator.get_next()

    def build_model_train(self):
        self.lr = tf.placeholder(tf.float32, name='learning_rate')

        # Input images
        self.domain_A = self.fetch_data(self.trainA_dataset)
        self.domain_B = self.fetch_data(self.trainB_dataset)

        # Forward generation
        self.x_ab = self.gen.translate(self.domain_A, scope='gen_a2b')

        # Backward generation
        if self.cyc_weight > 0.0:
            self.a_lr = tf.image.resize_images(self.domain_A, [self.resolution_bw, self.resolution_bw])
            self.ab_lr = tf.image.resize_images(self.x_ab, [self.resolution_bw, self.resolution_bw])
            self.aba_lr = self.gen_bw.translate(self.ab_lr, scope='gen_b2a')

        # Identity mapping
        self.x_bb = self.gen.translate(self.domain_B, reuse=True, scope='gen_a2b') \
            if self.id_weight > 0.0 else None

        # Discriminator
        b_logits = self.dis.discriminate(self.domain_B, scope='dis_b')
        ab_logits = self.dis.discriminate(self.x_ab, reuse=True, scope='dis_b')

        # Adversarial loss for G
        self.adv_loss_gen_ab = self.adv_weight * adv_loss(ab_logits, self.gan_type, target='real')

        # Adversarial loss for D
        self.adv_loss_dis_b = self.adv_weight * adv_loss(b_logits, self.gan_type, target='real')
        self.adv_loss_dis_b += self.adv_weight * adv_loss(ab_logits, self.gan_type, target='fake')

        # Identity loss
        self.id_loss_bb = self.id_weight * l1_loss(self.domain_B, self.x_bb) \
            if self.id_weight > 0.0 else 0.0
        self.cyc_loss_aba = self.cyc_weight * self.l1_loss(self.a_lr, self.aba_lr) \
            if self.cyc_weight > 0.0 else 0.0

        # Weight decay
        self.reg_loss_gen = self.reg_weight * regularization_loss('gen_')
        self.reg_loss_dis = self.reg_weight * regularization_loss('dis_')

        # Overall loss
        self.gen_loss_all = self.adv_loss_gen_ab + \
                            self.id_loss_bb + \
                            self.cyc_loss_aba + \
                            self.reg_loss_gen

        self.dis_loss_all = self.adv_loss_dis_b + \
                            self.reg_loss_dis


        """ Training """
        t_vars = tf.trainable_variables()
        G_vars = [var for var in t_vars if 'gen_' in var.name]
        D_vars = [var for var in t_vars if 'dis_' in var.name]

        self.G_optim = tf.train.AdamOptimizer(self.lr, beta1=0.5, beta2=0.999)\
            .minimize(self.gen_loss_all, var_list=G_vars)
        self.D_optim = tf.train.AdamOptimizer(self.lr, beta1=0.5, beta2=0.999)\
            .minimize(self.dis_loss_all, var_list=D_vars)

        """" Summary """
        # Record the IN scaling factor for each residual block.
        summary_scale_res = summary_by_keywords(['gamma', 'resblock', 'res2'], node_type='variable')
        summary_logits_gen = summary_by_keywords('pre_tanh', node_type='tensor')
        summary_logits_dis = summary_by_keywords(['D_logits_'], node_type='tensor')

        summary_list_gen = []
        summary_list_gen.append(tf.summary.scalar("gen_loss_all", self.gen_loss_all))
        summary_list_gen.append(tf.summary.scalar("adv_loss_gen_ab", self.adv_loss_gen_ab))
        summary_list_gen.append(tf.summary.scalar("reg_loss_gen", self.reg_loss_gen))
        summary_list_gen.extend(summary_scale_res)
        summary_list_gen.extend(summary_logits_gen)
        self.summary_gen = tf.summary.merge(summary_list_gen)

        summary_list_dis = []
        summary_list_dis.append(tf.summary.scalar("dis_loss_all", self.dis_loss_all))
        summary_list_dis.append(tf.summary.scalar("adv_loss_dis_b", self.adv_loss_dis_b))
        summary_list_dis.append(tf.summary.scalar("reg_loss_dis", self.reg_loss_dis))
        summary_list_dis.extend(summary_logits_dis)
        self.summary_dis = tf.summary.merge(summary_list_dis)