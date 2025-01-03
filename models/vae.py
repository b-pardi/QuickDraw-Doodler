import torch
from torch.optim import Adam, lr_scheduler
import torch.nn as nn
import torch.nn.functional as F
from torchinfo import summary
from torch.nn.utils.rnn import pack_padded_sequence

from tqdm import tqdm
from collections import defaultdict
from datetime import datetime
import os
import inspect
import numpy as np
import matplotlib.pyplot as plt

from models.mdn import MDN
from models.lstm import RecurDropLayerNormLSTM
from utils.process_data import get_real_samples_from_dataloader
from utils.image_rendering import animate_strokes
from utils.metrics_visualize import plot_generator_metrics, log_metrics, distribution_comparison
#torch.autograd.set_detect_anomaly(True)

class DoodleGenRNN(nn.Module):
    def __init__(
            self,
            in_size,                # 6: stroke_6 format (dx, dy, dt, p1, p2, p3)
            enc_hidden_size,        # encoder size of each layers hidden and cell state vectors
            dec_hidden_size,        # decoder size of each layers hidden and cell state vectors
            num_lstm_layers,        # number of layers for the encoder and decoder
            latent_size,            # size of latent vector z, encoder maps to z, decoder maps from z
            num_gmm_modes,          # number of modes for the Gaussian Mixture Model
            recurrent_dropout,      # fraction of recurrent connection nodes to drop to 0
            dropout,                # fraction of nodes to drop to 0
            use_layer_norm,         # boolean that will determine which decoder to use along with recurrent_dropout
            decoder_act,            # activation fn to apply to decoder output
        ):

        super(DoodleGenRNN, self).__init__()
        self.apply(init_weights)
        self.mdn = MDN(num_gmm_modes)

        self.in_size = in_size
        self.enc_hidden_size = enc_hidden_size
        self.dec_hidden_size = dec_hidden_size
        self.decoder_act = decoder_act
        self.num_gmm_modes = num_gmm_modes
        self.latent_size = latent_size

        # encode sequential stroke data with bidirectional LSTM
        # final hidden state of lstm defines the latent space
        self.lstm_encoder = nn.LSTM(
            input_size=in_size,
            hidden_size=enc_hidden_size,
            num_layers=num_lstm_layers,
            dropout=dropout,
            batch_first=True, # data is loaded with batch dim first (b, seq_len, points)
            bidirectional=True
        )

        # latent space which will be pulled from for generation
        # latent space -> compressed representation of input data to lower dimensional space
        # like convolutions grab most important features, this latent space will learn the same
        # essentially hidden lstm to latent space, but with reparameterizing to sample from gaussian dist of mean and var of hidden
        self.encoder_to_latent_mu_fc = nn.Linear(enc_hidden_size * 2, latent_size) # * 2 for bidirectionality
        self.encoder_to_latent_logvar_fc = nn.Linear(enc_hidden_size * 2, latent_size)

        # decoder LSTM
        self.latent_to_decoder_h_fc = nn.Linear(latent_size, dec_hidden_size)
        self.latent_to_decoder_c_fc = nn.Linear(latent_size, dec_hidden_size)
        if use_layer_norm or recurrent_dropout > 0.:
            # jit script allows torch to compile the model and run it much more efficiently
            self.lstm_decoder = torch.jit.script(
                RecurDropLayerNormLSTM(
                    input_size=in_size + latent_size,
                    hidden_size=dec_hidden_size,
                    num_layers=num_lstm_layers,
                    batch_first=True,
                    recurrent_dropout=recurrent_dropout,
                    use_layer_norm=use_layer_norm
                )
            )
        else:
            self.lstm_decoder = nn.LSTM(
                input_size=in_size + latent_size,
                hidden_size=dec_hidden_size,
                num_layers=num_lstm_layers,
                batch_first=True,
                dropout=dropout
            )

        # different input sizes for with and without attention mechanisms
        # output size is for Gaussian mixture density components
        #  -> 8 gaussian params (mu_dx, mu_dy, mu_dt, sigma_dx, sigma_dy. sigma_dt, rho (correlation), pi, (mixing coefficient))
        # num_gmm_modes -> how many modes the Mixture Density Network will predict (default 20)
        # +3 -> pen states 0, 1, 2
        self.decoder_to_gmm_fc = nn.Linear(dec_hidden_size, (8 * num_gmm_modes) + 3)


    def encode(self, x, lengths):
        """
        Encode input sequence of strokes into latent space 
        Final hidden state of lstm is the encoded sequence (compressed/summarized input)
        """
        # pack pad the sequence to uniform length
        x = pack_padded_sequence(x, lengths.cpu(), batch_first=True, enforce_sorted=False)

        # grab final hidden state of lstm encoder
        _, (hn, _) = self.lstm_encoder(x)
            
        hidden_final = hn[-2:] # grab last two hidden states (last of forward and last of backward)
        hn = hidden_final.permute(1,0,2).flatten(1).contiguous()  # reshape to (batch_size, 2*hidden_size)

        # mu and logvar aren't the actual mean and log of variance of hidden state,
        # rather they're learned params we will use as the "mean" and log of "variance" to sample z from
        mu = self.encoder_to_latent_mu_fc(hn) # mean of latent vec (z)
        logvar = self.encoder_to_latent_logvar_fc(hn) # log of variance of latent vec, use log for numerical stability

        return mu, logvar

    def reparameterize(self, mu, logvar):
        """
        Reparameterize: Sample z ~ N(mu, sigma^2)
        essentially sampling the latent space vector (z),
        from normal distribution defined by stats of encoder's hidden state
        
        allows for backpropagation of network,
        since directly sampling as described above is non-differentiable,
        due to the randomness involved in sampling,
        but reformulating the sampling is since epsilon contains the randomness,
        and is not part of the model's parameters.
        """
        sigma = torch.exp(0.5 * logvar) # standard deviation
        epsilon = torch.randn_like(sigma, device=sigma.device) # epsilon ~ N(0,1) -> random sample from normal dis
        z = mu + epsilon * sigma # adds some random var to latent space to increase generation diversity

        return z

    def decode(self, z, seq_len, inputs=None):
        """
        Decode the encoders's output while conditioning the latent vec on the embedded label
        """
        # learn a relationship between label and sample
        # only giving label of one class per sample, labels is plural due to batch processing
        # embedding learns relations indirectly since it is involved in the network,
        # but does not directly receive input sequences to associate with labels

        # prepare hidden state for decoder mapping from z to generate initial hidden state
        # unsqueeze to add dim of num_layers for decoder lstm since it expects (num_layers (1 initially), B, hidden_size)
        h0 = self.latent_to_decoder_h_fc(z).unsqueeze(0)
        c0 = self.latent_to_decoder_c_fc(z).unsqueeze(0)
        
        # apply appropriate activation to hidden state
        if self.decoder_act.lower() != 'none':
            activation = getattr(F, self.decoder_act.lower())
            h0 = activation(h0)
            c0 = activation(c0)

        # init hidden state from z that is mapped from the above fc layer is repeated across lstm layers
        h0 = h0.repeat(self.lstm_decoder.num_layers, 1, 1)
        c0 = c0.repeat(self.lstm_decoder.num_layers, 1, 1)
        hidden = (h0, c0)

        # if inputs isn't none,
        # it will contain the original input sequence
        # allowing the decoder to learn separately from the encoder's messy outputs.
        # Prepare inputs
        if inputs is None:
            batch_size = z.size(0)
            inputs = torch.zeros((batch_size, seq_len.size(0), self.in_size), device=z.device)

        # pass entire sequence through decoder
        # expand z to match_input dim
        batch_size, seq_length, _ = inputs.size()
        z_exp = z.unsqueeze(1).expand(batch_size, seq_length, z.size(-1))
        decoder_inputs = torch.cat((inputs, z_exp), dim=-1) # cat inputs and z for decoder
        decoder_output, _ = self.lstm_decoder(decoder_inputs, hidden)

        return decoder_output

    def sample_sketch(self, seq_len, z=None):
        self.eval()  # Set the model to evaluation mode
        device = next(self.parameters()).device

        if z is None:
            # sample from gaussian normal distribution latent vector
            z = torch.randn((1, self.latent_size), device=device)

        # Initialize hidden state for the decoder (same as beginning of decode method)
        h0 = self.latent_to_decoder_h_fc(z).unsqueeze(0)
        c0 = self.latent_to_decoder_c_fc(z).unsqueeze(0)
        if self.decoder_act.lower() != 'none':
            activation = getattr(F, self.decoder_act.lower())
            h0 = activation(h0)
            c0 = activation(c0)
        h0 = h0.repeat(self.lstm_decoder.num_layers, 1, 1)
        c0 = c0.repeat(self.lstm_decoder.num_layers, 1, 1)
        hidden = (h0, c0)

        # Start the sketch with a zero vector (SOS input token)
        input0 = torch.zeros((1, 1, self.in_size), device=z.device) # (1, 1, 6)

        #expand z to match input for concatenation
        z_exp = z.unsqueeze(1) # (1,1,latent_size)

        # cat SOS token and z
        input_t = torch.cat([input0, z_exp], dim=-1)
        
        sketch = []
        for _ in range(seq_len):
            # Forward pass through the decoder
            output, hidden = self.lstm_decoder(input_t, hidden)
            gmm_params = self.decoder_to_gmm_fc(output.squeeze(1)) # (1, (8*m)+3)

            # sample from GMM
            self.mdn.set_mixture_coeff(gmm_params)
            sampled_point = self.mdn.sample() # 1D tensor (dx, dy, dt, p1, p2, p3)
            sketch.append(sampled_point.unsqueeze(0))

            # prepare the next input as step we just sampled
            input_t = sampled_point.unsqueeze(0).unsqueeze(0).to(device) # (1, 1, 6)
            
            # concat z again for next input
            input_t = torch.cat([input_t, z_exp], dim=-1)

            # break early if EOS token reached
            pen_state_idx = torch.argmax(sampled_point[3:]).item()
            if pen_state_idx == 2:
                break

        sketch = torch.cat(sketch, dim=0) # (L, 6)
        #print(sketch, sketch.size())
        return sketch

    def forward(self, x, seq_len, labels=None, inputs=None):
        mu, logvar = self.encode(x, seq_len) # encode input sequence
        logvar = torch.clamp(logvar, min=-10, max=10)
        z = self.reparameterize(mu, logvar) # sample latent vector from encoded sequence
        decoder_output = self.decode(z, seq_len, inputs) # decode latent vector with label condition
        gmm_outputs = self.decoder_to_gmm_fc(decoder_output) # final fc layer connects decoder output to gmm params       

        return gmm_outputs, mu, logvar


def init_weights(model):
    if isinstance(model, nn.Linear): # fc layers init weights with xavier-glorot
        nn.init.xavier_normal_(model.weight)
        nn.init.constant_(model.bias, 0.) # bias init 0

    elif isinstance(model, nn.LSTM):
        for name, param in model.named_parameters():
            if 'weight_ih' in name: # input to hidden weights get xavier-glorot init
                nn.init.xavier_uniform_(param.data)

            elif 'weight_hh' in name: # hidden to hidden weights get orthogonal init
                nn.init.orthogonal_(param.data)

            elif 'bias' in name: # bias init 0
                nn.init.constant_(param.data, 0.)

def compute_anneal_factor(step, kl_weight_start, kl_decay_rate, kl_weight=1.0):
    '''returns the anneal factor including the kl_weight from model params'''
    anneal = 1 - (1 - kl_weight_start) * (kl_decay_rate ** step)
    return anneal * kl_weight

def kl_divergence_loss(mu, logvar, anneal_factor, tol=0.25):
    """
    Calculate KL divergence loss with annealing.
    Args:
        mu: Latent mean (batch_size, latent_size).
        logvar: Latent log-variance (batch_size, latent_size).
        anneal_factor: Weight for the KL loss.
    Returns:
        L_KL: Scalar KL divergence loss.
    """
    kl = -0.5 * torch.sum(1 + logvar - mu.pow(2) - logvar.exp())
    kl = torch.clamp(kl, min=tol)
    return anneal_factor * kl / mu.size(0)


def train(
        epoch,
        num_epochs,
        train_loader,
        rnn,
        optim,
        scheduler,
        grad_clip,
        anneal_factor,
        kl_tolerance,
        reg_covar, 
        device,
        metrics
):
    rnn.train()

    running_recon_train_loss, running_div_train_loss, running_total_train_loss = 0., 0., 0.
    latent_variances, unique_outputs, all_train_mus = [], [], []
    
    train_bar = tqdm(enumerate(train_loader), total=len(train_loader), desc=f"Training Epoch {epoch+1}/{num_epochs}")
    for batch_idx, (Xbatch, seq_lens, ybatch) in train_bar:
        Xbatch, seq_lens, ybatch = Xbatch.to(device), seq_lens.to(device), ybatch.to(device)

        # Teacher forcing:
        # decoder_inputs: the model sees the previous ground truth step as input
        # decoder_target: what we want the model to predict
        decoder_inputs = Xbatch[:, :-1, :]   # all but last step as input
        decoder_target = Xbatch[:, 1:, :]    # all but first step as target
        max_seq_len = decoder_inputs.size(1)

        # Forward pass
        gmm_outputs, mu, logvar = rnn.forward(Xbatch, seq_lens, inputs=decoder_inputs) # outputs: (B, max_seq_len, output_dim)  

        # Create a mask to ignore padding
        mask = torch.arange(max_seq_len, device=device).unsqueeze(0) < seq_lens.unsqueeze(1)

        # Reconstruction loss using MDN
        rnn.mdn.set_mixture_coeff(gmm_outputs) # Mixture coefficients for GMM
        rec_loss = rnn.mdn.reconstruction_loss(decoder_target, mask, reg_covar)

        kl_div_loss = kl_divergence_loss(mu, logvar, anneal_factor, kl_tolerance) # KL divergence loss

        loss = rec_loss + kl_div_loss # total loss

        optim.zero_grad() # zero previous iterations grads
        loss.backward() # back prop
        if grad_clip is not None:
            nn.utils.clip_grad_norm_(rnn.parameters(), grad_clip)
        optim.step() # take gradient step
        scheduler.step() # decrease lr

        # update metrics
        running_total_train_loss += loss.item()
        running_div_train_loss += kl_div_loss.item()
        running_recon_train_loss += rec_loss.item()
        
        train_bar.set_postfix(
            total_loss=running_total_train_loss / (batch_idx + 1),
            kl_loss=running_div_train_loss / (batch_idx + 1),
            recon_loss=running_recon_train_loss / (batch_idx + 1)
        )
    

    # log metrics
    n = len(train_loader)
    metrics['train']['total_loss'].append(running_total_train_loss / n)
    metrics['train']['kl_div_loss'].append(running_div_train_loss / n)
    metrics['train']['recon_loss'].append(running_recon_train_loss / n)


def validate(val_loader, rnn, device, metrics):
    rnn.eval()
    running_total_loss = 0.
    running_kl_loss = 0.
    running_recon_loss = 0.

    # No gradient calculation during validation
    with torch.no_grad():
        val_bar = tqdm(enumerate(val_loader), total=len(val_loader), desc="Validating")
        for batch_idx, (Xbatch, seq_lens, ybatch) in val_bar:
            Xbatch, seq_lens = Xbatch.to(device), seq_lens.to(device)
            ybatch = ybatch.to(device)

            # Teacher forcing in validation as well:
            decoder_inputs = Xbatch[:, :-1, :]
            decoder_target = Xbatch[:, 1:, :]
            max_seq_len = decoder_inputs.size(1)

            outputs, mu, logvar = rnn(Xbatch, seq_lens, inputs=decoder_inputs)  # (B, max_seq_len, output_dim)

            # Create mask
            mask = torch.arange(max_seq_len, device=device).unsqueeze(0) < seq_lens.unsqueeze(1)

            # Get mixture coeffs
            rnn.mdn.set_mixture_coeff(outputs)

            # Reconstruction loss
            rec_loss = rnn.mdn.reconstruction_loss(decoder_target, mask)
            # KL divergence (use the same anneal_factor as the last training step or a fixed factor = 1 if you want)
            # Typically in validation we might just use anneal_factor = 1 since we want to measure full KL
            kl_div_loss = kl_divergence_loss(mu, logvar, anneal_factor=1.0)

            loss = rec_loss + kl_div_loss

            running_total_loss += loss.item()
            running_kl_loss += kl_div_loss.item()
            running_recon_loss += rec_loss.item()

            val_bar.set_postfix(
                total_loss=running_total_loss / (batch_idx + 1),
                kl_loss=running_kl_loss / (batch_idx + 1),
                recon_loss=running_recon_loss / (batch_idx + 1)
            )
    # log metrics
    n = len(val_loader)
    metrics['val']['total_loss'].append(running_total_loss / n)
    metrics['val']['kl_div_loss'].append(running_kl_loss / n)
    metrics['val']['recon_loss'].append(running_recon_loss / n)

def train_rnn(train_loader, val_loader, subset_labels, device, rnn_config):    
    # get model's params and filter config file for them
    print("Initializing VAE and beginning training...")
    rnn_config.update({'num_labels': len(subset_labels)})
    rnn_config.update({'subset_labels': subset_labels})
    rnn_signature = inspect.signature(DoodleGenRNN.__init__)
    rnn_params = {k: v for k, v in rnn_config.items() if k in rnn_signature.parameters}

    rnn = DoodleGenRNN(**rnn_params).to(device)
    rnn.apply(init_weights)

    optim = Adam(rnn.parameters(), lr=rnn_config['learning_rate'])
    scheduler = lr_scheduler.ExponentialLR(optim, rnn_config['lr_decay'])

    # get shape of sample to give as input to summary
    for batch in train_loader:
        temp_sample_shape = batch[0].shape
        temp_seq_len = batch[1]
        temp_labels = batch[2]
        break

    model_summary = summary(rnn, input_size=temp_sample_shape, col_names=["input_size", "output_size", "num_params", "trainable"], verbose=0, seq_len=temp_seq_len, labels=None)
    print(model_summary)
    param_count = sum(p.numel() for p in rnn.parameters())
    print(f"Torch.jit.script makes model summary incorrectly count params\nACTUAL NUM PARAMETERS: {param_count}")
    
    # init metrics dict    
    train_metrics = defaultdict(list)
    val_metrics = defaultdict(list)
    test_metrics = defaultdict(list)
    metrics = {
        'train': train_metrics,
        'val': val_metrics,
        'test': test_metrics,
        'hyperparams': rnn_params,
        "device": str(device)
    }

    dist_metrics = {}

    spatial_scale_factor = val_loader.dataset.dxy_std
    temporal_scale_factor = val_loader.dataset.dt_std

    # for saving model and metrics
    start_time = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}" # for saving model
    model_fp = "output/model_ckpts/"
    log_dir = "output/model_ckpts/"
    os.makedirs(model_fp, exist_ok=True)

    # train/val loop
    global_step = 0
    for epoch in range(rnn_config['num_epochs']):
        weighted_anneal_factor = compute_anneal_factor(
            global_step,
            rnn_config['kl_weight_start'],
            rnn_config['kl_decay_rate'],
            kl_weight=rnn_config['kl_weight']
        )
        
        train(
            epoch,
            rnn_config['num_epochs'],
            train_loader,
            rnn,
            optim,
            scheduler,
            rnn_config['grad_clip'],
            weighted_anneal_factor,
            rnn_config['kl_tolerance'],
            rnn_config['reg_covar'],
            device,
            metrics
        )

        rnn.eval()
        validate(val_loader, rnn, device, metrics)

        global_step += len(train_loader)

        # basic log and metrics every epoch
        plot_generator_metrics(metrics, start_time, epoch+1, log_dir)
        log_metrics(metrics, f"DoodleGenRNN-losses-{start_time}.json", log_dir)

        if epoch % 5 == 0:
            # every 5 epochs save model, generate plot, log model summary, save model
            model_fn = f"DoodleGenRNN-epoch{epoch+1}-{start_time}"
            torch.save({
                'epoch': epoch + 1,
                'model_state_dict': rnn.state_dict(),
                'optimizer_state_dict': optim.state_dict(),
                'train_loss': metrics['train']['total_loss'][epoch],
                'val_loss': metrics['val']['total_loss'][epoch],
                'hyperparams': rnn_params
            }, model_fp + model_fn + '.pt')

            with open(model_fp + model_fn + '.log', 'w', encoding='utf-8') as model_summary_file:
                model_summary_file.write(str(model_summary))

        if epoch % 10 == 0:
            # distribution metrics every 10 epochs
            print("Analyzing real and generated data distributions...")
            
            seq_pt_count = 0
            sample_count = 0
            max_seq_points = 5000
            gen_x, gen_y, gen_t = [], [], []
            while seq_pt_count < max_seq_points:
                gen_sketch = rnn.sample_sketch(rnn_config['max_seq_len'])
                if sample_count < 3:
                    try:
                        gif_fp = f"{log_dir}/DoodleGenRNN-sample-sketch-epoch{epoch+1}-{sample_count}-{start_time}.gif"
                        animate_strokes(gen_sketch.numpy(), use_actual_time=True, save_gif=True, gif_fp=gif_fp, dxy_std=spatial_scale_factor, dt_std=temporal_scale_factor)
                    except Exception as e:
                        print(f"Error generate doodle animation: {e}")
                seq_pt_count += gen_sketch.size(0)
                sample_count += 1
                gen_dx = gen_sketch[:, 0].numpy().flatten()
                gen_dy = gen_sketch[:, 1].numpy().flatten()
                gen_dt = gen_sketch[:, 2].numpy().flatten()
                gen_x.append(gen_dx)
                gen_y.append(gen_dy)
                gen_t.append(gen_dt)
            gen_x = np.concatenate(gen_x)[:max_seq_points]
            gen_y = np.concatenate(gen_y)[:max_seq_points]
            gen_t = np.concatenate(gen_t)[:max_seq_points]

            real_x, real_y, real_t = get_real_samples_from_dataloader(val_loader, max_samples=max_seq_points)
            
            # init new figure for current iteration
            fig, ax = plt.subplots(3,1, figsize=(16, 12))
            fig.suptitle("Generator Metrics Over Epochs", fontsize=16)
            jsds, wds = [], []
            for i, (real_data, gen_data, data_var) in enumerate([(real_x, gen_x, 'dx'), (real_y, gen_y, 'dy'), (real_t, gen_t, 'dt')]):
                fig, ax, jsd, wd = distribution_comparison(fig, ax, i, real_data, gen_data, data_var, epoch+1, log_dir)
                jsds.append(round(jsd, 4))
                wds.append(round(wd, 4))
            fname = f"{log_dir}/DoodleGenRNN-distributions-epoch{epoch+1}-{start_time}.png"
            fig.savefig(fname, dpi=300) 
            dist_metrics[f"JSD-xyt-epoch-{epoch+1}"] = jsds
            dist_metrics[f"WD-xyt-epoch-{epoch+1}"] = wds
            log_metrics(dist_metrics, f"DoodleGenRNN-distributions-epoch{epoch+1}-{start_time}.log", log_dir)
            plt.close(fig)
            print("Done! Next epoch starting...")