#!/usr/bin/env python
# D. Jones, R. Kessler - 8/31/18
from __future__ import print_function

import os
import argparse
import configparser
import numpy as np
import sys
from salt3.util import snana
from salt3.util.estimate_tpk_bazin import estimate_tpk_bazin
from scipy.optimize import minimize, least_squares
from salt3.training import saltfit
from salt3.training.init_hsiao import init_hsiao
from astropy.io import fits

class TrainSALT:
	def __init__(self):
		self.warnings = []
	
	def addwarning(self,warning):
		print(warning)
		self.warnings.append(warning)
		
	def add_options(self, parser=None, usage=None, config=None):
		if parser == None:
			parser = argparse.ArgumentParser(usage=usage, conflict_handler="resolve")

		# The basics
		parser.add_argument('-v', '--verbose', action="count", dest="verbose",
							default=1,help='verbosity level')
		parser.add_argument('--debug', default=False, action="store_true",
							help='debug mode: more output and debug files')
		parser.add_argument('--clobber', default=False, action="store_true",
							help='clobber')
		parser.add_argument('-c','--configfile', default=None, type=str,
							help='configuration file')

		# input/output files
		parser.add_argument('--snlist', default=config.get('iodata','snlist'), type=str,
							help="""list of SNANA-formatted SN data files, including both photometry and spectroscopy. (default=%default)""")
		parser.add_argument('--speclist', default=config.get('iodata','speclist'), type=str,
							help="""optional list of ascii spectra, which will be written to the 
							SNANA-formatted SN light curve files provided by the snlist argument.
							List format should be space-delimited SNID, MJD-OBS (or DATE-OBS), spectrum filename (default=%default)""")
		parser.add_argument('--outputdir', default=config.get('iodata','outputdir'), type=str,
							help="""data directory for spectroscopy, format should be ASCII 
							with columns wavelength, flux, fluxerr (optional) (default=%default)""")
		parser.add_argument('--initmodelfile', default=config.get('iodata','initmodelfile'), type=str,
							help="""initial model to begin training, ASCII with columns
							phase, wavelength, flux (default=%default)""")
		parser.add_argument('--kcor_path', default=config.get('iodata','kcor_path'), type=str, action='append',
							help="""kcor_path gives survey,kcorfile for each survey used in the training data (default=%default)""")

		# training parameters
		parser.add_argument('--waverange', default=list(map(int,config.get('trainparams','waverange').split(','))), type=int, nargs=2,
							help='wavelength range over which the model is defined (default=%default)')
		parser.add_argument('--colorwaverange', default=list(map(int,config.get('trainparams','colorwaverange').split(','))), type=int, nargs=2,
							help='wavelength range over which the color law is fit to data (default=%default)')
		parser.add_argument('--interpfunc', default=config.get('trainparams','interpfunc'), type=str,
							help='function to interpolate between control points in the fitting (default=%default)')
		parser.add_argument('--interporder', default=config.get('trainparams','interporder'), type=int,
							help='for splines/polynomial funcs, order of the function (default=%default)')
		parser.add_argument('--wavesplineres', default=config.get('trainparams','wavesplineres'), type=float,
							help='number of angstroms between each wavelength spline knot (default=%default)')
		parser.add_argument('--phasesplineres', default=config.get('trainparams','phasesplineres'), type=float,
							help='number of angstroms between each phase spline knot (default=%default)')
		parser.add_argument('--waveoutres', default=config.get('trainparams','waveoutres'), type=float,
							help='wavelength resolution in angstroms of the output file (default=%default)')
		parser.add_argument('--phaseoutres', default=config.get('trainparams','phaseoutres'), type=float,
							help='phase resolution in angstroms of the output file (default=%default)')
		parser.add_argument('--phaserange', default=list(map(int,config.get('trainparams','phaserange').split(','))), type=int, nargs=2,
							help='phase range over which model is trained (default=%default)')
		parser.add_argument('--minmethod', default=config.get('trainparams','minmethod'), type=str, nargs=2,
							help='minimization algorithm, passed to scipy.optimize.minimize (default=%default)')
		parser.add_argument('--n_components', default=config.get('trainparams','n_components'), type=int,
							help='number of principal components of the SALT model to fit for (default=%default)')
		parser.add_argument('--n_colorpars', default=config.get('trainparams','n_colorpars'), type=int,
							help='number of degrees of the phase-independent color law polynomial (default=%default)')
		parser.add_argument('--n_specrecal', default=config.get('trainparams','n_specrecal'), type=int,
							help='number of parameters defining the spectral recalibration (default=%default)')
		
		return parser

	def rdkcor(self,kcorpath):

		self.kcordict = {}
		for k in kcorpath:
			survey,kcorfile = k.split(',')
			kcorfile = os.path.expandvars(kcorfile)
			if not os.path.exists(kcorfile):
				raise RuntimeError('kcor file %s does not exist'%kcorfile)
			self.kcordict[survey] = {}

			try:
				hdu = fits.open(kcorfile)
				zpoff = hdu[1].data
				snsed = hdu[2].data
				filtertrans = hdu[5].data
				primarysed = hdu[6].data
				hdu.close()
			except:
				raise RuntimeError('kcor file format is non-standard')

			self.kcordict[survey]['filtwave'] = filtertrans['wavelength (A)']
			self.kcordict[survey]['primarywave'] = primarysed['wavelength (A)']
			self.kcordict[survey]['snflux'] = snsed['SN Flux (erg/s/cm^2/A)']
			if 'AB' in primarysed.names:
				self.kcordict[survey]['AB'] = primarysed['AB']
			if 'Vega' in primarysed.names:
				self.kcordict[survey]['Vega'] = primarysed['Vega']
			for filt in zpoff['Filter Name']:
				self.kcordict[survey][filt.split('-')[-1]] = {}
				self.kcordict[survey][filt.split('-')[-1]]['filttrans'] = filtertrans[filt]
				self.kcordict[survey][filt.split('-')[-1]]['zpoff'] = zpoff['ZPOff(Primary)'][zpoff['Filter Name'] == filt][0]
				self.kcordict[survey][filt.split('-')[-1]]['magsys'] = zpoff['Primary Name'][zpoff['Filter Name'] == filt][0]

	def rdSpecData(self,datadict,speclist,tpk):

		if not os.path.exists(speclist):
			raise RuntimeError('speclist %s does not exist')
		
		snid,mjd,specfiles = np.loadtxt(speclist,unpack=True,dtype='str')
		snid,mjd,specfiles = np.atleast_1d(snid),np.atleast_1d(mjd),np.atleast_1d(specfiles)
		for s,m,sf in zip(snid,mjd,specfiles):
			try: m = float(m)
			except: m = snana.date_to_mjd(m)

			if '/' not in sf:
				sf = '%s/%s'%(os.path.dirname(speclist),sf)
			if not os.path.exists(sf):
				raise RuntimeError('specfile %s does not exist'%sf)
				
			if s in datadict.keys():
				if 'specdata' not in datadict[s].keys():
					datadict[s]['specdata'] = {}
					speccount = 0
				else:
					speccount = len(datadict[s]['specdata'].keys())
				datadict[s]['specdata'][speccount] = {}
				try:
					wave,flux,fluxerr = np.loadtxt(sf,unpack=True,usecols=[0,1,2])
					datadict[s]['specdata'][speccount]['fluxerr'] = fluxerr
				except:
					wave,flux = np.loadtxt(sf,unpack=True,usecols=[0,1])
				datadict[s]['specdata'][speccount]['wavelength'] = wave
				datadict[s]['specdata'][speccount]['flux'] = flux
				datadict[s]['specdata'][speccount]['tobs'] = m - tpk
				datadict[s]['specdata'][speccount]['mjd'] = m
			else:
				print('SNID %s has no photometry so I\'m ignoring it')

		return datadict

	def rdAllData(self,snlist,speclist=None):
		datadict = {}

		if not os.path.exists(snlist):
			raise RuntimeError('SN list %s doesn\'t exist'%snlist)
		snfiles = np.loadtxt(snlist,dtype='str')
		snfiles = np.atleast_1d(snfiles)

		for f in snfiles:
			if f.lower().endswith('.fits'):
				raise RuntimeError('FITS extensions are not supported yet')

			if '/' not in f:
				f = '%s/%s'%(os.path.dirname(snlist),f)
			sn = snana.SuperNova(f)

			if sn.SNID in datadict.keys():
				self.addwarning('SNID %s is a duplicate!  Skipping'%sn.SNID)
				continue

			if not 'SURVEY' in sn.__dict__.keys():
				raise RuntimeError('File %s has no SURVEY key, which is needed to find the filter transmission curves'%PhotSNID[0])
			if not 'REDSHIFT_HELIO' in sn.__dict__.keys():
				raise RuntimeError('File %s has no heliocentric redshift information in the header'%PhotSNID[0])
			
			zHel = float(sn.REDSHIFT_HELIO.split('+-')[0])
			tpk,tpkmsg = estimate_tpk_bazin(sn.MJD,sn.FLUXCAL,sn.FLUXCALERR)
			if 'termination condition is satisfied' not in tpkmsg:
				self.addwarning('skipping SN %s; can\'t estimate t_max')
				continue

			datadict[sn.SNID] = {'snfile':f,
								 'zHelio':zHel,
								 'survey':sn.SURVEY}
			#datadict[snid]['zHelio'] = zHel
			
			# TODO: flux errors
			datadict[sn.SNID]['specdata'] = {}
			for k in sn.SPECTRA.keys():
				datadict[sn.SNID]['specdata'][k] = {}
				datadict[sn.SNID]['specdata'][k]['specphase'] = sn.SPECTRA[k]['SPECTRUM_MJD']
				datadict[sn.SNID]['specdata'][k]['tobs'] = sn.SPECTRA[k]['SPECTRUM_MJD'] - tpk
				datadict[sn.SNID]['specdata'][k]['mjd'] = sn.SPECTRA[k]['SPECTRUM_MJD']
				if 'LAMAVG' in sn.SPECTRA[k].keys():
					datadict[sn.SNID]['specdata'][k]['wavelength'] = sn.SPECTRA[k]['LAMAVG']
				elif 'LAMMIN' in sn.SPECTRA[k].keys() and 'LAMMAX' in sn.SPECTRA[k].keys():
					datadict[sn.SNID]['specdata'][k]['wavelength'] = np.mean([[sn.SPECTRA[k]['LAMMIN']],
																			  [sn.SPECTRA[k]['LAMMAX']]],axis=0)
				else:
					raise RuntimeError('couldn\t find wavelength data in photometry file')
				datadict[sn.SNID]['specdata'][k]['flux'] = sn.SPECTRA[k]['FLAM']
				datadict[sn.SNID]['specdata'][k]['fluxerr'] = sn.SPECTRA[k]['FLAMERR']
				
			datadict[sn.SNID]['photdata'] = {}
			datadict[sn.SNID]['photdata']['tobs'] = sn.MJD - tpk
			datadict[sn.SNID]['photdata']['mjd'] = sn.MJD
			datadict[sn.SNID]['photdata']['fluxcal'] = sn.FLUXCAL
			datadict[sn.SNID]['photdata']['fluxcalerr'] = sn.FLUXCALERR
			datadict[sn.SNID]['photdata']['filt'] = sn.FLT

		if speclist:
			datadict = self.rdSpecData(datadict,speclist,tpk)
			
		return datadict

	def fitSALTModel(self,datadict,phaserange,phaseres,waverange,waveres,
					 colorwaverange,minmethod,kcordict,initmodelfile,phaseoutres,waveoutres,
					 n_components=1,n_colorpars=0):

		n_phaseknots = int((phaserange[1]-phaserange[0])/phaseres)-4
		n_waveknots = int((waverange[1]-waverange[0])/waveres)-4
		n_sn = len(datadict.keys())

		# x1,x0,c for each SN
		# phase/wavelength spline knots for M0, M1 (ignoring color for now)
		# TODO: spectral recalibration
		n_params = n_components*n_phaseknots*n_waveknots + 4*n_sn
		if n_colorpars: n_params += n_colorpars
		
		guess = np.zeros(n_params)

		if not os.path.exists(initmodelfile):
			from salt3.initfiles import init_rootdir
			initmodelfile = '%s/%s'%(init_rootdir,initmodelfile)
		if not os.path.exists(initmodelfile):
			raise RuntimeError('model initialization file not found in local directory or %s'%init_rootdir)
			
		phase,wave,m0,m1,m0knots,m1knots = init_hsiao(
			initmodelfile,phaserange=phaserange,waverange=waverange,
			phasesplineres=phaseres,wavesplineres=waveres,
			phaseinterpres=phaseoutres,waveinterpres=waveoutres)

		parlist = ['m0']*(n_phaseknots*n_waveknots)
		if n_components == 2:
			parlist += ['m1']*(n_phaseknots*n_waveknots)
		if n_colorpars:
			parlist += ['cl']*n_colorpars

		for k in datadict.keys():
			parlist += ['x0_%s'%k,'x1_%s'%k,'c_%s'%k,'tpkoff_%s'%k]
		parlist = np.array(parlist)

		
		guess[parlist == 'm0'] = m0knots
		if n_components == 2:
			guess[parlist == 'm1'] = m1knots
		if n_colorpars:
			guess[parlist == 'cl'] = [0.]*n_colorpars
		guess[(parlist == 'm0') & (guess < 0)] = 0

		saltfitter = saltfit.chi2(guess,datadict,parlist,phaserange,
								  waverange,phaseres,waveres,phaseoutres,waveoutres,
								  colorwaverange,
								  kcordict,n_components,n_colorpars)

		# first pass - estimate x0 so we can bound it to w/i an order of mag
		initbounds = ([0,-np.inf,-np.inf,-5]*n_sn,[np.inf,np.inf,np.inf,5]*n_sn)
		initguess = (1,0,0,0)*n_sn #+ (0,)*n_colorpars
		initparlist = []
		for k in datadict.keys():
			initparlist += ['x0_%s'%k,'x1_%s'%k,'c_%s'%k,'tpkoff_%s'%k]
		#initparlist += ['cl']*n_colorpars
		initparlist = np.array(initparlist)
		saltfitter.parlist = initparlist

		md_init = least_squares(saltfitter.chi2fit,initguess,method='trf',bounds=initbounds,
								args=(True,))
		try:
			if 'condition is satisfied' not in md_init.message.decode('utf-8'):
				self.addwarning('Initialization minimizer message: %s'%md_init.message)
		except:
			if 'condition is satisfied' not in md_init.message:
				self.addwarning('Initialization minimizer message: %s'%md_init.message)
		if self.verbose:
			print('x0 guesses initialized successfully')


		# 2nd pass - let the SALT model spline knots float			
		lsqbounds_lower = [-np.inf]*(n_components*n_phaseknots*n_waveknots + n_colorpars)
		for k in datadict.keys():
			lsqbounds_lower += [md_init.x[initparlist == 'x0_%s'%k]*1e-1,-np.inf,-np.inf,-5]
		lsqbounds_upper = [np.inf]*(n_components*n_phaseknots*n_waveknots+n_colorpars)
		for k in datadict.keys():
			lsqbounds_upper += [md_init.x[initparlist == 'x0_%s'%k]*1e1,np.inf,np.inf,5]

			
		lsqbounds = (lsqbounds_lower,lsqbounds_upper)
		for k in datadict.keys():
			guess[parlist == 'x0_%s'%k] = md_init.x[initparlist == 'x0_%s'%k]
		#guess[parlist == 'cl'] = md_init.x[initparlist == 'cl']
			
		saltfitter.parlist = parlist

		# lsmr is for sparse problems, and regularize option defaults to true
		# this is presumably less optimal than what SALT2 does
		md = least_squares(saltfitter.chi2fit,guess,method='trf',
						   bounds=lsqbounds,args=(False,False,False))#,tr_solver='lsmr')

		# another fitting option, but least_squares seems to
		# work best for now
		#md = minimize(saltfitter.chi2fit,guess,
		#			  bounds=lsqbounds,
		#			  method=minmethod,
		#			  options={'maxiter':100000,'maxfev':100000,'maxfun':100000})

		try:
			if 'condition is satisfied' not in md.message.decode('utf-8'):
				self.addwarning('Minimizer message: %s'%md.message)
		except:
			if 'condition is satisfied' not in md.message:
				self.addwarning('Minimizer message: %s'%md.message)
				
		phase,wave,M0,M1,clpars,SNParams = \
			saltfitter.getPars(md.x)

		return phase,wave,M0,M1,clpars,SNParams

	def wrtoutput(self,outdir,phase,wave,M0,M1,clpars,SNParams):

		if not os.path.exists(outdir):
			raise RuntimeError('desired output directory %s doesn\'t exist'%outdir)

		# principal components and color law
		foutm0 = open('%s/salt3_template_0.dat'%outdir,'w')
		foutm1 = open('%s/salt3_template_1.dat'%outdir,'w')
		foutcl = open('%s/salt3_color_correction.dat'%outdir,'w')
		
		for p,i in zip(phase,range(len(phase))):
			for w,j in zip(wave,range(len(wave))):
				print('%.1f %.2f %8.5e'%(p,w,M0[i,j]),file=foutm0)
				print('%.1f %.2f %8.5e'%(p,w,M1[i,j]),file=foutm1)

		foutm0.close()
		foutm1.close()

		print('%i'%len(clpars),file=foutcl)
		for c in clpars:
			print('%8.5e'%c,file=foutcl)
		print("""Salt2ExtinctionLaw.version 1
Salt2ExtinctionLaw.min_lambda %i
Salt2ExtinctionLaw.max_lambda %i"""%(
	self.options.colorwaverange[0],
	self.options.colorwaverange[1]),file=foutcl)
		foutcl.close()

		# best-fit SN params
		foutsn = open('%s/salt3train_snparams.txt'%outdir,'w')
		print('# SN x0 x1 c t0',file=foutsn)
		for k in SNParams.keys():
			print('%s %8.5e %.4f %.4f %.2f'%(k,SNParams[k]['x0'],SNParams[k]['x1'],SNParams[k]['c'],SNParams[k]['t0']),file=foutsn)
		foutsn.close()
			
		return

	def main(self):

		if not self.options.kcor_path:
			raise RuntimeError('kcor_path variable must be defined!')
		self.rdkcor(self.options.kcor_path)
		# TODO: ASCII filter files
		
		# read the data
		datadict = self.rdAllData(self.options.snlist,speclist=self.options.speclist)
		
		# fit the model - initial pass
		phase,wave,M0,M1,clpars,SNParams = self.fitSALTModel(
			datadict,self.options.phaserange,self.options.phasesplineres,
			self.options.waverange,self.options.wavesplineres,
			self.options.colorwaverange,
			self.options.minmethod,self.kcordict,
			self.options.initmodelfile,
			self.options.phaseoutres,self.options.waveoutres,
			self.options.n_components,
			self.options.n_colorpars)
		
		# write the output model - M0, M1, c
		self.wrtoutput(self.options.outputdir,phase,wave,M0,M1,clpars,SNParams)

		print('successful SALT2 training!  Output files written to %s'%self.options.outputdir)
		
	
if __name__ == "__main__":
	usagestring = """SALT3 Training

usage: python TrainSALT.py -c <configfile> <options>

config file options can be overwridden at the command line

Dependencies: sncosmo?
"""

	if sys.version_info < (3,0):
		sys.exit('Sorry, Python 2 is not supported')
	
	salt = TrainSALT()

	parser = argparse.ArgumentParser(usage=usagestring, conflict_handler="resolve")
	parser.add_argument('-c','--configfile', default=None, type=str,
					  help='configuration file')
	options, args = parser.parse_known_args()

	if options.configfile:
		config = configparser.ConfigParser()
		if not os.path.exists(options.configfile):
			raise RuntimeError('Configfile doesn\'t exist!')
		config.read(options.configfile)
	else:
		parser.print_help()
		raise RuntimeError('Configuration file must be specified at command line')

	parser = salt.add_options(usage=usagestring,config=config)
	options = parser.parse_args()

	options.kcor_path = (options.kcor_path,)
	with open(options.configfile) as fin:
		for line in fin:
			if line.startswith('kcor_path+'):
				options.kcor_path += (line.replace('\n','').split('=')[-1],)
	
	salt.options = options
	salt.verbose = options.verbose
	salt.clobber = options.clobber
	
	salt.main()

	if len(salt.warnings):
		print('There were warnings!!')
		print(salt.warnings)

